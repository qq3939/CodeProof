"""报告生成（SPEC 3.1 阅卷与报告、8.4 时机）。

所有题都判完后再生成报告；报告本身不调用模型——总分、雷达图、薄弱点都是从阅卷结果
确定性汇总出来的，省钱且可复现。同时在这里更新掌握度。
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.agents.grader import grade_question
from app.core.config import get_settings
from app.core.db import session_scope
from app.mastery import service as mastery
from app.models import (
    ExamSession,
    Grading,
    Job,
    Question,
    Report,
    RepoSnapshot,
    SessionTurn,
    Topic,
)

logger = logging.getLogger(__name__)

KIND_LABEL = {
    "design": "设计决策",
    "risk": "风险点",
    "chain": "链路",
    "mechanism": "机制",
    "resume": "简历说法",
    "change": "改动理解",
    "kb": "八股联系",
}
VERDICT_LABEL = {"hit": "✅ 命中", "partial": "🟡 部分命中", "miss": "❌ 未命中"}


async def _asked_questions(session_id: uuid.UUID) -> list[uuid.UUID]:
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(SessionTurn.question_id, SessionTurn.meta)
                .where(SessionTurn.session_id == session_id, SessionTurn.role == "examiner")
                .order_by(SessionTurn.seq)
            )
        ).all()
    out: list[uuid.UUID] = []
    for qid, meta in rows:
        if qid and (meta or {}).get("kind") == "question" and qid not in out:
            out.append(qid)
    return out


async def _wait_grade_jobs(session_id: uuid.UUID, timeout: float = 90) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with session_scope() as db:
            pending = (
                await db.execute(
                    select(Job.id).where(
                        Job.kind == "grade",
                        Job.args["session_id"].astext == str(session_id),
                        Job.status.in_(["queued", "running"]),
                    )
                )
            ).all()
        if not pending:
            return
        await asyncio.sleep(1)


async def build_report(session_id: uuid.UUID) -> dict[str, Any]:
    s = get_settings()
    await _wait_grade_jobs(session_id)
    qids = await _asked_questions(session_id)
    async with session_scope() as db:
        graded = {
            g.question_id for g in (await db.execute(select(Grading).where(Grading.session_id == session_id))).scalars()
        }
    for qid in qids:
        if qid not in graded:
            await grade_question(session_id, qid)

    async with session_scope() as db:
        sess = await db.get(ExamSession, session_id)
        assert sess is not None
        snap = await db.get(RepoSnapshot, sess.snapshot_id)
        assert snap is not None
        gradings = {
            g.question_id: g
            for g in (await db.execute(select(Grading).where(Grading.session_id == session_id))).scalars()
        }
        questions = {q.id: q for q in (await db.execute(select(Question).where(Question.id.in_(qids)))).scalars()}
        topics = {
            t.id: t
            for t in (
                await db.execute(
                    select(Topic).where(Topic.id.in_([q.topic_id for q in questions.values() if q.topic_id]))
                )
            ).scalars()
        }
        items = []
        by_kind: dict[str, list[float]] = defaultdict(list)
        for i, qid in enumerate(qids, 1):
            q, g = questions.get(qid), gradings.get(qid)
            if q is None or g is None:
                continue
            t = topics.get(q.topic_id) if q.topic_id else None
            kind = t.kind if t else (q.topic_key or "").split(":")[0].split(".")[0]
            kind = (
                kind
                if kind in KIND_LABEL
                else {"kb_link": "kb"}.get(sess.mode, sess.mode if sess.mode in KIND_LABEL else "mechanism")
            )
            by_kind[kind].append(g.score)
            items.append(
                {"index": i, "question": q, "grading": g, "title": t.title if t else (q.topic_key or ""), "kind": kind}
            )
        scores = [it["grading"].score for it in items]
        total = round(statistics.mean(scores), 1) if scores else 0.0
        radar = {KIND_LABEL.get(k, k): round(statistics.mean(v), 1) for k, v in by_kind.items()}
        weak = [
            {
                "topic_key": it["question"].topic_key,
                "title": it["title"],
                "score": it["grading"].score,
                "review": it["grading"].review_topics,
            }
            for it in items
            if it["grading"].score < s.pass_score
        ]
        verdict = None
        if sess.mode == "change":
            verdict = "理解这次改动" if total >= s.pass_score else "还没有充分理解这次改动"
        md = render_markdown(sess, items, total, radar, weak, verdict)
        payload = {
            "total": total,
            "radar": radar,
            "count": len(items),
            "verdict": verdict,
            "per_question": [
                {"question_id": str(it["question"].id), "score": it["grading"].score, "kind": it["kind"]}
                for it in items
            ],
        }
        existing = await db.get(Report, session_id)
        if existing:
            existing.summary_md, existing.scores, existing.weak_topics = md, payload, weak
        else:
            db.add(Report(session_id=session_id, summary_md=md, scores=payload, weak_topics=weak))
        # 掌握度：按考点记录本次得分
        now = datetime.now(UTC)
        for it in items:
            key = it["question"].topic_key
            if key:
                await mastery.record(
                    db,
                    sess.user_id,
                    snap.repository_id,
                    key,
                    float(it["grading"].score),
                    str(session_id),
                    it["title"],
                    now,
                )
        sess.status = "finished"
        sess.finished_at = sess.finished_at or now
        await db.commit()
    return payload


def render_markdown(
    sess: ExamSession,
    items: list[dict[str, Any]],
    total: float,
    radar: dict[str, float],
    weak: list[dict[str, Any]],
    verdict: str | None,
) -> str:
    lines = [
        "# 考核报告",
        "",
        f"- 模式：{sess.mode}　难度：{sess.difficulty}　题数：{len(items)}",
        f"- 总分：**{total}**",
        f"- 结束原因：{sess.finish_reason or '-'}",
    ]
    if verdict:
        lines.append(f"- 结论：**{verdict}**")
    lines += ["", "## 各类考点得分", ""] + [f"- {k}：{v}" for k, v in radar.items()]
    for it in items:
        q, g = it["question"], it["grading"]
        lines += ["", f"## 第 {it['index']} 题（{g.score} 分）{it['title']}", "", q.text, "", "### 要点命中", ""]
        for pr in g.point_results:
            kp = q.key_points[pr["index"]] if pr["index"] < len(q.key_points) else {"point": "?", "evidence": []}
            ev = kp["evidence"][0] if kp.get("evidence") else None
            loc = f"`{ev['path']}:{ev['start_line']}-{ev['end_line']}`" if ev else ""
            lines.append(
                f"- {VERDICT_LABEL.get(pr['verdict'], pr['verdict'])} {kp['point']} {loc}　— {pr.get('reason', '')}"
            )
        if g.contradictions:
            lines += ["", "### 与代码矛盾之处", ""]
            for c in g.contradictions:
                evs = "、".join(f"`{e['path']}:{e['start_line']}`" for e in c.get("evidence", []))
                lines.append(f"- 「{c['claim']}」：{c['explanation']} {evs}")
        lines += [
            "",
            "### 反馈",
            "",
            g.feedback_md,
            "",
            "<details><summary>参考讲解</summary>",
            "",
            q.reference_md,
            "",
            "</details>",
        ]
    lines += ["", "## 薄弱点与复习建议", ""]
    if weak:
        for w in weak:
            lines.append(f"- {w['title']}（{w['score']} 分）：建议复习 {', '.join(w['review']) or w['topic_key']}")
    else:
        lines.append("- 本场没有低于及格线的考点，继续保持。")
    return "\n".join(lines)
