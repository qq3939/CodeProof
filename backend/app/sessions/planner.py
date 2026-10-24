"""出题计划：不同模式从不同来源取题（SPEC 3.2）。

计划在创建会话时就定下来（题目顺序、考哪些考点），存在 sessions.config["plan"]；
但按需生成的题（链路、八股联系、简历、改动）在轮到它时才真正调用模型生成，
这样创建会话很快，而且用户提前结束时不会浪费 Token。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.analysis.generation import DEEP_DIVE_KINDS, generate_questions
from app.analysis.languages import primary_language
from app.core.db import session_scope
from app.core.errors import AppError
from app.llm.types import CallContext
from app.mastery import service as mastery
from app.models import (
    ClaimVerification,
    ExamSession,
    Mastery,
    Question,
    RepoProfile,
    RepoSnapshot,
    ResumeClaim,
    Topic,
)
from app.retrieval.service import retrieve
from app.tools.sandbox import Sandbox

MODES = {"deep_dive", "kb_link", "resume", "chain", "change"}


async def build_plan(sess: ExamSession) -> list[dict[str, Any]]:
    """按模式定出题计划，并给每一项带上仓库主语言：出题和面试官的提示词按它填写（D-021）。"""
    plan = await _plan_items(sess)
    lang = await snapshot_language(sess.snapshot_id)
    for item in plan:
        item["lang"] = lang
    return plan


async def snapshot_language(snapshot_id: uuid.UUID) -> str | None:
    """仓库主语言：分析时写进画像；画像里没有（改动之前分析的快照）就按快照的语言统计现算。"""
    async with session_scope() as db:
        prof = await db.get(RepoProfile, snapshot_id)
        if prof is not None and "primary_language" in (prof.profile or {}):
            return prof.profile["primary_language"]
        snap = await db.get(RepoSnapshot, snapshot_id)
        return primary_language(snap.lang_stats or {}) if snap is not None else None


async def _plan_items(sess: ExamSession) -> list[dict[str, Any]]:
    async with session_scope() as db:
        snap = await db.get(RepoSnapshot, sess.snapshot_id)
        assert snap is not None
        ms = await mastery.load(db, sess.user_id, snap.repository_id)
        n = sess.question_count
        if sess.mode == "deep_dive":
            rows = (
                await db.execute(
                    select(Question, Topic)
                    .join(Topic, Topic.id == Question.topic_id)
                    .where(
                        Question.snapshot_id == sess.snapshot_id, Question.mode == "deep_dive", Question.scope.is_(None)
                    )
                )
            ).all()
            items = [
                {
                    "question_id": str(q.id),
                    "topic_key": q.topic_key or t.topic_key,
                    "kind": t.kind,
                    "importance": t.importance,
                    "difficulty": q.difficulty,
                    "title": t.title,
                }
                for q, t in rows
            ]
            if not items:
                raise AppError("这个快照还没有题库，请等分析完成", code="no_questions")
            return pick_deep_dive(items, n, ms, sess.difficulty)
        if sess.mode == "kb_link":
            topics = (
                (
                    await db.execute(
                        select(Topic).where(
                            Topic.snapshot_id == sess.snapshot_id, Topic.kind.in_(["mechanism", "design", "risk"])
                        )
                    )
                )
                .scalars()
                .all()
            )
            items = [
                {
                    "gen": _topic_dict(t),
                    "topic_key": f"kb:{t.topic_key}",
                    "kind": t.kind,
                    "importance": t.importance,
                    "title": t.title,
                }
                for t in topics
            ]
            if not items:
                raise AppError("这个仓库没有识别到可以联系八股的机制用法", code="no_topics")
            return mastery.pick(items, n, ms, sess.difficulty)
        if sess.mode == "chain":
            endpoint = (sess.config or {}).get("endpoint")
            prof = await db.get(RepoProfile, sess.snapshot_id)
            chain = next(
                (c for c in (prof.profile.get("chains", []) if prof else []) if c["endpoint"] == endpoint), None
            )
            if chain is None:
                raise AppError("请选择一个存在的接口", code="no_chain")
            steps = [s for s in chain["steps"] if s.get("file")]
            plan = []
            for i, st in enumerate(steps[: max(n, 2)]):
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                plan.append(
                    {
                        "gen": {
                            "kind": "chain",
                            "title": f"{endpoint} 第 {i + 1} 站：{st['symbol']}（{st['layer']}）",
                            "description": f"这一站从 {st.get('from', '入口')} 调用而来" if i else "链路入口",
                            "evidence": [
                                {
                                    "path": st["file"],
                                    "start_line": st["start_line"],
                                    "end_line": min(st["end_line"], st["start_line"] + 39),
                                }
                            ],
                            "importance": 4,
                            "topic_key": f"chain:{endpoint}#{i + 1}",
                            "identifiers": [st["symbol"].split(".")[-1]],
                        },
                        "topic_key": f"chain:{endpoint}#{i + 1}",
                        "kind": "chain",
                        "title": f"第 {i + 1} 站：{st['symbol']}",
                        "next_station": nxt,
                    }
                )
            return plan
        if sess.mode == "resume":
            resume_id = (sess.config or {}).get("resume_id")
            if not resume_id:
                raise AppError("简历追问模式需要先选择一份简历", code="no_resume")
            claims = (
                (
                    await db.execute(
                        select(ResumeClaim).where(
                            ResumeClaim.resume_id == uuid.UUID(resume_id),
                            ResumeClaim.snapshot_id == sess.snapshot_id,
                            ResumeClaim.claim_type != "metric",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not claims:
                raise AppError(
                    "这份简历里没有对应到这个仓库的说法，请先在简历页设置项目与仓库的对应关系", code="no_claims"
                )
            vers = {
                v.claim_id: v
                for v in (
                    await db.execute(
                        select(ClaimVerification).where(ClaimVerification.claim_id.in_([c.id for c in claims]))
                    )
                ).scalars()
            }
            items = []
            for c in claims:
                v = vers.get(c.id)
                items.append(
                    {
                        "gen": {
                            "kind": "resume",
                            "title": c.claim_text[:180],
                            "description": f"简历项目「{c.project_name}」中的说法",
                            "evidence": (v.evidence if v else [])[:3],
                            "importance": 4 if v and v.verdict != "supported" else 3,
                            "topic_key": f"resume:{c.id}",
                            "identifiers": c.entities[:3],
                            "claim": c.claim_text,
                        },
                        "topic_key": f"resume:{c.id}",
                        "kind": "resume",
                        "importance": 4,
                        "title": c.claim_text[:60],
                    }
                )
            return mastery.pick(items, n, ms, sess.difficulty)
        if sess.mode == "change":
            topics = (sess.config or {}).get("change_topics", [])
            return [{"gen": t, "topic_key": t["topic_key"], "kind": "change", "title": t["title"]} for t in topics[:n]]
    raise AppError(f"未知模式 {sess.mode}")


def pick_deep_dive(
    items: list[dict[str, Any]], n: int, ms: dict[str, Mastery], difficulty: str
) -> list[dict[str, Any]]:
    """项目深挖只考设计决策和风险点（SPEC 3.2、D-019）。

    v1 从整个题库里挑，题库又被机制题和链路题占了大半，考出来像代码阅读题。
    这两类凑不够题量时（小仓库常见）才用其他类型的题补齐，免得开不了考。
    """
    primary = [i for i in items if i["kind"] in DEEP_DIVE_KINDS]
    chosen = mastery.pick(primary, n, ms, difficulty)
    if len(chosen) < n:
        rest = [i for i in items if i["kind"] not in DEEP_DIVE_KINDS]
        chosen += mastery.pick(rest, n - len(chosen), ms, difficulty)
    return chosen


def _topic_dict(t: Topic) -> dict[str, Any]:
    return {
        "kind": t.kind,
        "title": t.title,
        "description": t.description,
        "evidence": t.evidence,
        "importance": t.importance,
        "topic_key": t.topic_key,
        "identifiers": [],
    }


def _scope(sess: ExamSession) -> str:
    if sess.mode == "kb_link":
        return f"user:{sess.user_id}"
    if sess.mode == "resume":
        return f"resume:{(sess.config or {}).get('resume_id')}"
    if sess.mode == "change":
        return f"change:{sess.id}"
    return "chain"


def _qdict(q: Question, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": str(q.id),
        "text": q.text,
        "key_points": q.key_points,
        "followup_hints": q.followup_hints,
        "topic_key": q.topic_key or item["topic_key"],
        "title": item.get("title"),
        "kind": item.get("kind"),
        "next_station": item.get("next_station"),
        "lang": item.get("lang"),
    }


async def materialize(
    item: dict[str, Any], sess: ExamSession, sandbox: Sandbox, ctx: CallContext
) -> dict[str, Any] | None:
    """把计划项变成一道具体的题：题库里有就直接用，否则现场生成并缓存（scope 区分归属）。"""
    async with session_scope() as db:
        if "question_id" in item:
            q = await db.get(Question, uuid.UUID(item["question_id"]))
            return _qdict(q, item) if q else None
        scope = _scope(sess)
        cached = (
            await db.execute(
                select(Question)
                .where(
                    Question.snapshot_id == sess.snapshot_id,
                    Question.scope == scope,
                    Question.topic_key == item["topic_key"],
                    Question.mode == sess.mode,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if cached is not None:
            return _qdict(cached, item)
    topic = dict(item["gen"])
    kb = None
    extra_snippets: list[dict[str, Any]] = []
    if sess.mode == "kb_link":
        from app.kb.service import search

        kb = await search(sess.user_id, topic["title"] + " " + topic.get("description", ""), top_k=3)
    if not topic.get("evidence"):
        # 简历说法没有核验证据时，现场检索一次
        res = await retrieve(topic.get("claim") or topic["title"], sandbox, ctx.child("retrieve"))
        topic["evidence"] = [{"path": h.path, "start_line": h.start_line, "end_line": h.end_line} for h in res.hits[:3]]
        if not topic["evidence"]:
            return None
    extra = ""
    if sess.mode == "resume":
        extra = f"候选人简历里的原话：「{topic.get('claim', topic['title'])}」。围绕这句话和代码是否对得上来出题。"
    elif sess.mode == "change":
        extra = "这是改动理解模式：围绕改动意图、副作用、边界情况和测试出题。\n" + topic.get("diff_excerpt", "")[:3000]
    qs, tokens, model_name = await generate_questions(
        sandbox,
        topic,
        ctx.child("questions"),
        mode=sess.mode,
        difficulty=sess.difficulty,
        kb=kb,
        extra_snippets=extra_snippets,
        extra_context=extra,
        lang=item.get("lang"),
    )
    if not qs:
        return None
    q = qs[0]
    async with session_scope() as db:
        row = Question(
            snapshot_id=sess.snapshot_id,
            topic_id=None,
            mode=sess.mode,
            difficulty=q["difficulty"],
            text=q["text"],
            key_points=q["key_points"],
            followup_hints=q["followup_hints"],
            reference_md=q["reference_md"],
            topic_key=item["topic_key"],
            scope=_scope(sess),
            model=model_name,
            tokens=tokens,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return _qdict(row, item)
