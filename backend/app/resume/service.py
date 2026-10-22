"""简历核验（SPEC 9.1）。

1. 解析：pypdf 取文字（只支持文字版 PDF），快模型抽出项目和说法，分三类：技术使用 / 效果描述 / 数据指标；
2. 用户把每个项目对应到一个已分析的仓库快照；
3. 逐条核验：规划检索词 → 检索入口找证据 → 强模型给结论和修改建议；
4. 数据指标类说法：结论固定为"代码中无法验证"，提示准备测法说明；仓库里有压测报告就附上位置。
"""

from __future__ import annotations

import io
import logging
import re
import uuid
from typing import Any

from pypdf import PdfReader
from sqlalchemy import delete, select

from app.agents.schemas import QueryPlan, ResumeExtractOut, VerdictOut
from app.core.db import session_scope
from app.core.errors import AppError
from app.llm.client import get_llm, wrap_data
from app.llm.types import CallContext, LLMError
from app.models import ClaimVerification, RepoProfile, Resume, ResumeClaim
from app.repos.storage import ensure_checkout
from app.retrieval.service import retrieve
from app.tools.sandbox import Sandbox

logger = logging.getLogger(__name__)
BENCH_GLOBS = ["**/*压测*", "**/*benchmark*", "**/*Benchmark*", "**/*.jmx", "**/*perf*.md", "**/*wrk*", "**/*jmeter*"]

EXTRACT_SYSTEM = """从简历文本中抽出项目经历，把每个项目拆成一条条可以拿代码检查的说法。
每条说法标类型：tech 技术使用（用了什么技术/怎么实现）、effect 效果描述（解决了什么问题）、metric 数据指标（带数字的性能/规模）。
entities 填可以在代码里搜索的实体：类名、技术名（Redis、RabbitMQ）、注解、配置键、依赖名。"""

PLAN_SYSTEM = "为一条简历说法规划在代码仓库里检索的关键词：类名、方法名、注解、依赖名、配置键。输出 1-6 个。"

VERDICT_SYSTEM = """根据检索到的代码证据，判断简历说法和代码对不对得上：
supported 有证据 / partial 部分有 / not_found 找不到 / contradicted 与代码矛盾（例如简历写 Kafka，代码里用的是 RabbitMQ）。
evidence 只能引用给出的证据位置。suggestion 给出具体的修改建议：怎么改措辞才和代码一致，或者需要补充什么。"""


def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # noqa: BLE001
        raise AppError(f"PDF 解析失败：{e}") from e
    if len(text.strip()) < 20:
        raise AppError("没有从 PDF 里读到文字。只支持文字版 PDF，扫描版请直接粘贴文本", code="scanned_pdf")
    return text


async def create_resume(user_id: uuid.UUID, filename: str | None, text: str) -> uuid.UUID:
    ctx = CallContext(node="resume.extract", user_id=user_id)
    out, _ = await get_llm().structured(
        task="resume.extract",
        tier="fast",
        system=EXTRACT_SYSTEM,
        user=wrap_data("resume", text[:20000]),
        schema=ResumeExtractOut,
        ctx=ctx,
        payload={"text": text},
    )
    async with session_scope() as db:
        r = Resume(user_id=user_id, filename=filename, text=text, mapping={})
        db.add(r)
        await db.flush()
        for proj in out.projects:
            for c in proj.claims:
                db.add(
                    ResumeClaim(
                        resume_id=r.id,
                        project_name=proj.name[:300],
                        claim_text=c.text,
                        claim_type=c.type,
                        entities=c.entities,
                    )
                )
        await db.commit()
        return r.id


async def set_mapping(resume: Resume, mapping: dict[str, str | None]) -> None:
    async with session_scope() as db:
        r = await db.get(Resume, resume.id)
        assert r is not None
        r.mapping = {k: v for k, v in mapping.items() if v}
        claims = (await db.execute(select(ResumeClaim).where(ResumeClaim.resume_id == r.id))).scalars().all()
        for c in claims:
            sid = mapping.get(c.project_name)
            c.snapshot_id = uuid.UUID(sid) if sid else None
        await db.commit()


async def _grep_count(sandbox: Sandbox, term: str) -> tuple[int, list[dict[str, Any]]]:
    out = await sandbox.call("grep", {"pattern": re.escape(term)}, node="resume.verify")
    hits = []
    for line in out.splitlines():
        m = re.match(r"^([^:]+):(\d+):", line)
        if m:
            ln = int(m.group(2))
            hits.append({"path": m.group(1), "start_line": ln, "end_line": ln})
    return len(hits), hits


async def verify_claim(
    claim: ResumeClaim, sandbox: Sandbox, present_terms: list[str], ctx: CallContext
) -> dict[str, Any]:
    llm = get_llm()
    if claim.claim_type == "metric":
        bench = []
        for g in BENCH_GLOBS:
            out = await sandbox.glob(g)
            bench += [p for p in out.splitlines() if p and not p.startswith("(")]
        ev = [{"path": p, "start_line": 1, "end_line": 1} for p in sorted(set(bench))[:3]]
        return {
            "verdict": "unverifiable",
            "evidence": ev,
            "explanation": "这是数据指标类说法，代码里无法验证" + ("；仓库里找到了疑似压测材料" if ev else ""),
            "suggestion": "准备好测法说明：压测工具、机器配置、数据量、并发数、优化前后的对比方式"
            + ("，可以引用仓库里的压测报告" if ev else ""),
        }
    try:
        plan, _ = await llm.structured(
            task="resume.plan_queries",
            tier="fast",
            system=PLAN_SYSTEM,
            user=wrap_data("claim", claim.claim_text),
            schema=QueryPlan,
            ctx=ctx,
            payload={"claim": claim.claim_text, "entities": claim.entities},
        )
        queries = plan.queries
    except LLMError:
        queries = claim.entities or [claim.claim_text[:20]]
    hits: dict[str, int] = {}
    evidence: list[dict[str, Any]] = []
    for q in queries[:6]:
        n, locs = await _grep_count(sandbox, q)
        hits[q] = n
        evidence += locs[:2]
    res = await retrieve(claim.claim_text, sandbox, ctx.child("retrieve"), top_k=3)
    evidence = [{"path": h.path, "start_line": h.start_line, "end_line": h.end_line} for h in res.hits] + evidence
    ev_text = "\n\n".join(f"--- {h.path}:{h.start_line}-{h.end_line}\n{h.text[:1200]}" for h in res.hits)
    hit_text = "\n".join(f"- {k}: {v} 处匹配" for k, v in hits.items())
    user = (
        f"{wrap_data('claim', claim.claim_text)}\n\n关键词命中情况：\n{hit_text}\n\n仓库技术栈：{', '.join(present_terms)}\n\n"
        f"{wrap_data('evidence', ev_text)}"
    )
    out, _ = await llm.structured(
        task="resume.verdict",
        tier="strong",
        system=VERDICT_SYSTEM,
        user=user,
        schema=VerdictOut,
        ctx=ctx,
        payload={"hits": hits, "evidence": evidence[:5], "present_terms": present_terms},
        temperature=0,
    )
    d = out.model_dump()
    if not d["evidence"] and d["verdict"] in ("supported", "partial"):
        d["evidence"] = evidence[:3]
    return d


async def verify_resume(resume_id: uuid.UUID, job_id: str | None = None) -> None:
    async with session_scope() as db:
        r = await db.get(Resume, resume_id)
        assert r is not None
        claims = (await db.execute(select(ResumeClaim).where(ResumeClaim.resume_id == resume_id))).scalars().all()
        r.status = "verifying"
        await db.commit()
    ctx = CallContext(node="resume.verify", user_id=r.user_id, job_id=job_id)
    sandboxes: dict[uuid.UUID, tuple[Sandbox, list[str]]] = {}
    results = []
    for c in claims:
        if c.snapshot_id is None:
            continue
        if c.snapshot_id not in sandboxes:
            root = await ensure_checkout(c.snapshot_id)
            async with session_scope() as db:
                prof = await db.get(RepoProfile, c.snapshot_id)
            st = prof.profile.get("stack", {}) if prof else {}
            terms = list(st.get("tech", [])) + [d.split(":")[-1] for d in st.get("dependencies", [])]
            sandboxes[c.snapshot_id] = (Sandbox(root, c.snapshot_id, ctx=ctx), terms)
        sb, terms = sandboxes[c.snapshot_id]
        try:
            v = await verify_claim(c, sb, terms, ctx)
        except LLMError as e:
            v = {"verdict": "not_found", "evidence": [], "explanation": f"核验失败：{e}", "suggestion": "稍后重试"}
        results.append((c.id, v))
    async with session_scope() as db:
        await db.execute(delete(ClaimVerification).where(ClaimVerification.claim_id.in_([c.id for c in claims])))
        for cid, v in results:
            db.add(
                ClaimVerification(
                    claim_id=cid,
                    verdict=v["verdict"],
                    evidence=v["evidence"],
                    explanation=v["explanation"],
                    suggestion=v["suggestion"],
                )
            )
        row = await db.get(Resume, resume_id)
        assert row is not None
        row.status = "verified"
        await db.commit()
