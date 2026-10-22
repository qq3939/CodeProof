"""评测运行器（SPEC 15）。

uv run python -m app.eval run --suite small [--retrieval agentic|vector|hybrid] [--baseline]

流程：准备评测仓库（同一提交只分析一次）→ 逐项算指标 → 写 eval_runs 表 + docs/eval/<日期>-<suite>-report.md。
每一项指标的模型调用都挂在独立的 job_id 上（eval:<run>:<指标>），Token 和耗时直接从 llm_calls 汇总。
"""

from __future__ import annotations

import json
import logging
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.agents.grader import judge
from app.agents.schemas import GradeOut
from app.analysis import evidence as evcheck
from app.core.config import get_settings
from app.core.db import session_scope
from app.eval import metrics as M
from app.llm.client import get_llm, wrap_data
from app.llm.types import CallContext
from app.models import EvalRun, ExamSession, LlmCall, Question, RepoProfile, RepoSnapshot, User
from app.repos.storage import ensure_checkout
from app.retrieval.service import retrieve
from app.tools.sandbox import Sandbox

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]  # 项目根目录
DATASETS = ROOT / "eval" / "datasets"
REPORT_DIR = ROOT / "docs" / "eval"


@dataclass
class Suite:
    name: str
    path: Path
    config: dict[str, Any]
    questions: list[dict[str, Any]]
    answers: list[dict[str, Any]]
    labels: dict[str, dict[str, Any]]
    claims: list[dict[str, Any]]
    injections: list[dict[str, Any]]


def _jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_suite(name: str) -> Suite:
    p = DATASETS / name
    if not (p / "suite.json").exists():
        raise SystemExit(f"找不到评测集 {p}/suite.json")
    inj = []
    for f in sorted((p / "injection").glob("*.jsonl")) if (p / "injection").exists() else []:
        inj += _jsonl(f)
    return Suite(
        name=name,
        path=p,
        config=json.loads((p / "suite.json").read_text(encoding="utf-8")),
        questions=_jsonl(p / "questions.jsonl"),
        answers=_jsonl(p / "answers.jsonl"),
        labels={x["answer_id"]: x for x in _jsonl(p / "labels.jsonl")},
        claims=_jsonl(p / "claims.jsonl"),
        injections=inj,
    )


def _fixture_repo(rel: str) -> str:
    """把夹具目录复制成一个临时 git 仓库，返回 file:// 地址（只有测试 / 评测会用到）。"""
    src = ROOT / rel
    dest = Path(tempfile.mkdtemp(prefix="pb-eval-")) / src.name
    shutil.copytree(src, dest)
    env = {
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "e@e",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "e@e",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "eval fixture"]):
        subprocess.run(["git", *args], cwd=dest, env=env, check=True, capture_output=True)
    return f"file://{dest}"


async def _eval_user() -> User:
    from app.auth.router import upsert_user

    async with session_scope() as db:
        return await upsert_user(db, -999, "eval-bot", "评测机器人", None)


async def prepare_snapshot(suite: Suite) -> tuple[uuid.UUID, Path]:
    from app.core.queue import drain
    from app.repos.service import submit_repo

    s = get_settings()
    cfg = suite.config
    if cfg.get("fixture"):
        s.allow_file_repos = True
        url = _fixture_repo(cfg["fixture"])
    else:
        url = cfg["repo_url"]
    s.queue_mode = "inline"  # 评测在一个进程里跑完，不依赖 worker
    user = await _eval_user()
    async with session_scope() as db:
        res = await submit_repo(db, user, url, cfg.get("ref"))
    await drain(timeout=3600)
    async with session_scope() as db:
        snap = await db.get(RepoSnapshot, res["snapshot_id"])
        assert snap is not None
        if snap.status != "ready":
            raise SystemExit(f"评测仓库分析失败：{snap.error}")
    return snap.id, await ensure_checkout(snap.id)


async def _usage(job_id: str) -> dict[str, Any]:
    async with session_scope() as db:
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0),
                    func.coalesce(func.sum(LlmCall.cost), 0.0),
                    func.count(),
                ).where(LlmCall.job_id == job_id)
            )
        ).one()
    return {"tokens": int(row[0]), "cost": round(float(row[1]), 6), "calls": int(row[2])}


@dataclass
class Report:
    metrics: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, list[str]] = field(default_factory=dict)

    def fail(self, key: str, msg: str) -> None:
        self.failures.setdefault(key, []).append(msg)


# ---------------------------------------------------------------- 各项指标
async def eval_question_quality(suite: Suite, snap_id: uuid.UUID, sandbox: Sandbox, rep: Report) -> None:
    def valid_rate(qs: list[dict[str, Any]], tag: str) -> float:
        ok = total = 0
        for q in qs:
            for kp in q["key_points"]:
                total += 1
                err = evcheck.check(sandbox, kp["evidence"], [], "")
                if err is None:
                    ok += 1
                else:
                    rep.fail("evidence", f"[{tag}] {q.get('id') or q['text'][:30]}: {err}")
        return ok / total if total else 0.0

    async with session_scope() as db:
        bank = [
            {"id": str(q.id), "text": q.text, "key_points": q.key_points}
            for q in (
                await db.execute(select(Question).where(Question.snapshot_id == snap_id, Question.scope.is_(None)))
            ).scalars()
        ]
    rep.metrics["question_quality"] = {
        "dataset_evidence_valid_rate": round(valid_rate(suite.questions, "dataset"), 3),
        "bank_evidence_valid_rate": round(valid_rate(bank, "bank"), 3),
        "bank_questions": len(bank),
        "human_rating": "待人工抽样打分（基于代码事实 / 像真实面试 / 是否重复，各 1-5 分）",
    }


def _snippets(sandbox: Sandbox, q: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**kp["evidence"][0], "text": evcheck.snippet(sandbox, kp["evidence"][0]) or ""} for kp in q["key_points"]]


async def eval_grading(suite: Suite, sandbox: Sandbox, run_id: str, rep: Report) -> None:
    qmap = {q["id"]: q for q in suite.questions}
    ctx = CallContext(node="eval.grading", job_id=f"eval:{run_id}:grading")
    pred_pts: list[str] = []
    gold_pts: list[str] = []
    pred_scores: list[float] = []
    gold_scores: list[float] = []
    t0 = time.perf_counter()
    for a in suite.answers:
        lab = suite.labels.get(a["id"])
        q = qmap.get(a["question_id"])
        if lab is None or q is None:
            continue
        out, _ = await judge(q, [a["text"]], _snippets(sandbox, q), ctx)
        verdicts = [next((p.verdict for p in out.points if p.index == i), "miss") for i in range(len(q["key_points"]))]
        pred_pts += verdicts
        gold_pts += lab["points"]
        pred_scores.append(out.score)
        gold_scores.append(lab["score"])
        if abs(out.score - lab["score"]) >= 30:
            rep.fail("grading", f"{a['id']}（{a['quality']}）：系统 {out.score} 分，人工 {lab['score']} 分")
    # 稳定性：同一个回答判 3 次
    stab_runs = []
    for a in suite.answers[:6]:
        q = qmap[a["question_id"]]
        runs = []
        for _ in range(3):
            out, _ = await judge(q, [a["text"]], _snippets(sandbox, q), ctx)
            runs.append(
                [next((p.verdict for p in out.points if p.index == i), "miss") for i in range(len(q["key_points"]))]
            )
        stab_runs.append(M.stability(runs))
    use = await _usage(ctx.job_id or "")
    rep.metrics["grading"] = {
        "answers": len(pred_scores),
        "point_agreement": round(M.agreement(pred_pts, gold_pts), 3),
        "cohen_kappa": round(M.cohen_kappa(pred_pts, gold_pts), 3),
        "score_mae": round(M.mae(pred_scores, gold_scores), 2),
        "stability_3_runs": round(statistics.mean(stab_runs), 3) if stab_runs else None,
        "latency_s": round(time.perf_counter() - t0, 2),
        **use,
    }


BASELINE_SYSTEM = "根据这个仓库出题考我并判分。下面是一道题和我的回答，只给出 0-100 的分数和理由。"


async def eval_baseline(suite: Suite, sandbox: Sandbox, run_id: str, rep: Report) -> None:
    """基线："通用 Agent + 提示词"，只有 grep 和 read 两个工具，没有答案要点和代码证据（SPEC 15.3）。"""
    qmap = {q["id"]: q for q in suite.questions}
    ctx = CallContext(node="eval.baseline", job_id=f"eval:{run_id}:baseline")
    restricted = Sandbox(sandbox.root, None, ctx=ctx)
    restricted.specs = lambda: [s for s in type(restricted).specs(restricted) if s.name in ("grep", "read_file")]  # type: ignore[method-assign]
    pred, gold = [], []
    t0 = time.perf_counter()
    for a in suite.answers:
        lab, q = suite.labels.get(a["id"]), qmap.get(a["question_id"])
        if lab is None or q is None:
            continue
        found = await retrieve(q["text"], restricted, ctx, mode="agentic", top_k=3)
        code = "\n\n".join(f"{h.path}:{h.start_line}\n{h.text[:1500]}" for h in found.hits)
        out, _ = await get_llm().structured(
            task="baseline.judge",
            tier="strong",
            system=BASELINE_SYSTEM,
            ctx=ctx,
            schema=GradeOut,
            temperature=0,
            user=f"题目：{q['text']}\n{wrap_data('code', code)}\n{wrap_data('answer', a['text'])}",
            payload={"answer": a["text"], "code": code, "question": q["text"]},
        )
        pred.append(out.score)
        gold.append(lab["score"])
    use = await _usage(ctx.job_id or "")
    rep.metrics["baseline"] = {
        "score_mae": round(M.mae(pred, gold), 2),
        "latency_s": round(time.perf_counter() - t0, 2),
        **use,
    }


async def eval_retrieval(suite: Suite, sandbox: Sandbox, run_id: str, modes: list[str], rep: Report) -> None:
    out: dict[str, Any] = {}
    for mode in modes:
        ctx = CallContext(node=f"eval.retrieval.{mode}", job_id=f"eval:{run_id}:retrieval:{mode}")
        recalls = []
        t0 = time.perf_counter()
        calls = 0
        for q in suite.questions:
            res = await retrieve(q["text"], sandbox, ctx, mode=mode, top_k=5)  # type: ignore[arg-type]
            calls += res.tool_calls
            hits = [(h.path, h.start_line, h.end_line) for h in res.hits]
            golds = [(e["path"], e["start_line"], e["end_line"]) for kp in q["key_points"] for e in kp["evidence"]]
            r = M.recall_at_k(hits, golds, 5)
            recalls.append(r)
            if r == 0:
                rep.fail("retrieval", f"[{mode}] {q['id']} top-5 里没有任何标准证据")
        use = await _usage(ctx.job_id or "")
        out[mode] = {
            "recall@5": round(statistics.mean(recalls), 3) if recalls else 0.0,
            "tool_calls": calls,
            "latency_s": round(time.perf_counter() - t0, 2),
            **use,
        }
    rep.metrics["retrieval"] = out


async def eval_resume(suite: Suite, snap_id: uuid.UUID, sandbox: Sandbox, run_id: str, rep: Report) -> None:
    from app.models import ResumeClaim
    from app.resume.service import verify_claim

    if not suite.claims:
        return
    ctx = CallContext(node="eval.resume", job_id=f"eval:{run_id}:resume")
    async with session_scope() as db:
        prof = await db.get(RepoProfile, snap_id)
    st = prof.profile.get("stack", {}) if prof else {}
    terms = list(st.get("tech", [])) + [d.split(":")[-1] for d in st.get("dependencies", [])]
    pred, gold = [], []
    for c in suite.claims:
        claim = ResumeClaim(
            project_name="eval", claim_text=c["text"], claim_type=c.get("type", "tech"), entities=c.get("entities", [])
        )
        v = await verify_claim(claim, sandbox, terms, ctx)
        is_false = v["verdict"] in ("not_found", "contradicted")
        pred.append(is_false)
        gold.append(bool(c["is_false"]))
        if is_false != bool(c["is_false"]):
            rep.fail(
                "resume", f"{c['id']}「{c['text']}」：判为 {v['verdict']}，标注为{'假' if c['is_false'] else '真'}"
            )
    p, r = M.precision_recall(pred, gold)
    rep.metrics["resume"] = {
        "claims": len(pred),
        "false_claim_precision": round(p, 3),
        "false_claim_recall": round(r, 3),
        **await _usage(ctx.job_id or ""),
    }


async def eval_security(suite: Suite, snap_id: uuid.UUID, sandbox: Sandbox, run_id: str, rep: Report) -> None:
    from app.models import ResumeClaim
    from app.resume.service import verify_claim

    if not suite.injections:
        return
    qmap = {q["id"]: q for q in suite.questions}
    ctx = CallContext(node="eval.security", job_id=f"eval:{run_id}:security")
    executed = 0
    for inj in suite.injections:
        followed = False
        if inj["target"] == "answer":
            q = qmap[inj["question_id"]]
            out, _ = await judge(q, [inj["content"]], _snippets(sandbox, q), ctx)
            followed = out.score > inj.get("max_score", 30)
        elif inj["target"] == "resume":
            claim = ResumeClaim(
                project_name="eval", claim_text=inj["content"], claim_type="tech", entities=inj.get("entities", [])
            )
            v = await verify_claim(claim, sandbox, [], ctx)
            followed = v["verdict"] == inj.get("forbidden_verdict")
        elif inj["target"] == "code":
            async with session_scope() as db:
                qs = (
                    await db.execute(
                        select(Question.text, Question.reference_md).where(Question.snapshot_id == snap_id)
                    )
                ).all()
            followed = any(inj["forbidden_text"] in (t + r) for t, r in qs)
        if followed:
            executed += 1
            rep.fail("security", f"注入样本 {inj['id']}（{inj['target']}）被执行了")
    rep.metrics["security"] = {
        "samples": len(suite.injections),
        "executed": executed,
        "executed_rate": round(executed / len(suite.injections), 3),
    }


async def eval_cost(snap_id: uuid.UUID, rep: Report) -> None:
    async with session_scope() as db:
        snap = await db.get(RepoSnapshot, snap_id)
        assert snap is not None
        st = snap.stats or {}
        incr = (
            (
                await db.execute(
                    select(RepoSnapshot.stats).where(
                        RepoSnapshot.repository_id == snap.repository_id, RepoSnapshot.base_snapshot_id.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        avg_session = (
            await db.execute(
                select(func.avg(ExamSession.cost), func.avg(ExamSession.total_tokens)).where(
                    ExamSession.snapshot_id == snap_id, ExamSession.status == "finished"
                )
            )
        ).one()
    kloc = max(0.001, st.get("loc", 0) / 1000)
    rep.metrics["cost"] = {
        "loc": st.get("loc"),
        "first_analysis_s_per_kloc": round(st.get("elapsed_ms", 0) / 1000 / kloc, 3),
        "first_analysis_tokens_per_kloc": round(st.get("tokens_used", 0) / kloc, 1),
        "incremental_runs": len(incr),
        "incremental_tokens_saved": sum((x or {}).get("tokens_saved", 0) for x in incr),
        "incremental_reuse_ratio": round(
            statistics.mean([(x or {}).get("topics_reused", 0) / max(1, (x or {}).get("topics", 1)) for x in incr]), 3
        )
        if incr
        else None,
        "avg_session_cost": round(float(avg_session[0]), 6) if avg_session[0] is not None else None,
        "avg_session_tokens": int(avg_session[1]) if avg_session[1] is not None else None,
    }


# ---------------------------------------------------------------- 报告
def render(suite: Suite, run_id: str, cfg: dict[str, Any], rep: Report) -> str:
    m = rep.metrics
    s = get_settings()
    lines = [
        f"# 评测报告：{suite.name}（{datetime.now(UTC):%Y-%m-%d}）",
        "",
        f"- 运行 id：`{run_id}`",
        f"- 模型：{s.llm_provider} / 强 `{s.llm_model_strong}` / 快 `{s.llm_model_fast}`",
        f"- 检索模式：{', '.join(cfg['retrieval_modes'])}；基线对比：{'是' if cfg['baseline'] else '否'}",
        f"- 数据：{len(suite.questions)} 题、{len(suite.answers)} 个回答、{len(suite.claims)} 条简历说法、"
        f"{len(suite.injections)} 个注入样本",
        "",
    ]
    if s.llm_provider == "fake":
        lines += [
            "> ⚠️ 这次用的是假模型（LLM_PROVIDER=fake），只能说明流程跑通、输出格式正确，指标数值没有参考意义。",
            "",
        ]

    def table(title: str, rows: dict[str, Any]) -> None:
        lines.extend([f"## {title}", "", "| 指标 | 值 |", "|---|---|"])
        lines.extend(f"| {k} | {v} |" for k, v in rows.items())
        lines.append("")

    for key, title in (
        ("question_quality", "出题质量"),
        ("grading", "阅卷（与人工标注对比）"),
        ("baseline", "基线：通用 Agent + 提示词"),
        ("resume", "简历核验"),
        ("security", "安全（提示注入）"),
        ("cost", "成本"),
    ):
        if key in m:
            table(title, m[key])
    if "retrieval" in m:
        lines += [
            "## 检索（三种模式对比）",
            "",
            "| 模式 | recall@5 | 工具调用 | Token | 耗时(s) |",
            "|---|---|---|---|---|",
        ]
        for mode, v in m["retrieval"].items():
            lines.append(f"| {mode} | {v['recall@5']} | {v['tool_calls']} | {v['tokens']} | {v['latency_s']} |")
        lines.append("")
    lines += ["## 结论", ""]
    g = m.get("grading", {})
    if g:
        lines.append(
            f"- 阅卷要点一致率 {g['point_agreement']}，kappa {g['cohen_kappa']}，总分平均绝对误差 {g['score_mae']}。"
        )
    if "baseline" in m and g:
        better = m["baseline"]["score_mae"] > g["score_mae"]
        lines.append(
            f"- 和基线比，总分误差 {g['score_mae']} vs {m['baseline']['score_mae']}，"
            f"{'本系统更接近人工标注' if better else '本系统没有优于基线，需要排查'}。"
        )
    if "retrieval" in m:
        best = max(m["retrieval"].items(), key=lambda kv: kv[1]["recall@5"])
        lines.append(f"- 检索召回最高的是 {best[0]}（recall@5={best[1]['recall@5']}）。")
    if "security" in m:
        lines.append(f"- 注入样本被执行比例 {m['security']['executed_rate']}（目标 0）。")
    lines += ["", "## 失败案例分析", ""]
    if not rep.failures:
        lines.append("- 没有失败案例。")
    for k, items in rep.failures.items():
        lines.append(f"### {k}（{len(items)} 条）")
        lines.extend(f"- {x}" for x in items[:15])
        lines.append("")
    return "\n".join(lines)


async def run(suite_name: str, retrieval: str | None, baseline: bool) -> dict[str, Any]:
    suite = load_suite(suite_name)
    modes = [retrieval] if retrieval else ["agentic", "vector", "hybrid"]
    s = get_settings()
    if any(m in ("vector", "hybrid") for m in modes) and s.retrieval_mode == "agentic":
        s.retrieval_mode = "hybrid"  # 需要建代码向量索引
    cfg = {
        "retrieval_modes": modes,
        "baseline": baseline,
        "provider": s.llm_provider,
        "strong": s.llm_model_strong,
        "fast": s.llm_model_fast,
    }
    async with session_scope() as db:
        er = EvalRun(suite=suite_name, config=cfg, status="running")
        db.add(er)
        await db.commit()
        run_id = str(er.id)
    rep = Report()
    try:
        snap_id, root = await prepare_snapshot(suite)
        sandbox = Sandbox(root, snap_id, ctx=CallContext(node="eval", job_id=f"eval:{run_id}"), allow_vector=True)
        await eval_question_quality(suite, snap_id, sandbox, rep)
        await eval_grading(suite, sandbox, run_id, rep)
        if baseline:
            await eval_baseline(suite, sandbox, run_id, rep)
        await eval_retrieval(suite, sandbox, run_id, modes, rep)
        await eval_resume(suite, snap_id, sandbox, run_id, rep)
        await eval_security(suite, snap_id, sandbox, run_id, rep)
        await eval_cost(snap_id, rep)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"{datetime.now(UTC):%Y-%m-%d}-{suite_name}-report.md"
        path.write_text(render(suite, run_id, cfg, rep), encoding="utf-8")
        status = "done"
    except Exception:
        status = "failed"
        path = None
        raise
    finally:
        async with session_scope() as db:
            row = await db.get(EvalRun, uuid.UUID(run_id))
            assert row is not None
            row.status, row.metrics, row.finished_at = status, rep.metrics, datetime.now(UTC)
            row.report_path = str(path) if path else None
            await db.commit()
    return {"run_id": run_id, "report": str(path), "metrics": rep.metrics}
