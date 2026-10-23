"""仓库分析流水线（SPEC 第 7 节），在 worker 里运行。

    queued → cloning → indexing → profiling → generating → ready

增量分析（SPEC 7.7）：同一仓库已有 ready 的旧快照时，按文件内容哈希比对：
- 解析结果按内容哈希缓存，没变的文件不重新解析；
- 代码块向量按 (路径, 内容哈希) 复用；
- 证据全部落在没变文件里的考点和题目，直接复制到新快照；
- 画像总是重新汇总（它依赖全局调用关系，而且是零成本的确定性计算）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select

from app.analysis.candidates import build_candidates
from app.analysis.code_model import CodeModel, FileEntry
from app.analysis.generation import generate_questions, generate_topics
from app.analysis.java_index import Annotation, Call, JavaFileIndex, Symbol, parse_java
from app.analysis.spring_profile import TextFile, build_profile, profile_summary_md
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import job_id_var
from app.llm.client import get_llm
from app.llm.types import CallContext
from app.models import (
    CodeChunk,
    CodeFile,
    CodeRef,
    CodeSymbol,
    Question,
    RepoProfile,
    Repository,
    RepoSnapshot,
    Topic,
)
from app.repos import git
from app.repos.progress import set_status
from app.tools.sandbox import SKIP_DIRS, Sandbox, is_sensitive, snapshot_root

logger = logging.getLogger(__name__)

LANG_BY_EXT = {
    ".java": "java",
    ".kt": "kotlin",
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".vue": "vue",
    ".xml": "xml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".properties": "properties",
    ".sql": "sql",
    ".md": "markdown",
    ".go": "go",
    ".html": "html",
    ".css": "css",
    ".gradle": "gradle",
    ".json": "json",
    ".sh": "shell",
}
MAX_TOPICS_WITH_QUESTIONS = 20
QUESTIONS_PER_TOPIC = 1


class AnalysisError(Exception):
    pass


# ---------------------------------------------------------------- 解析缓存
_parse_cache: dict[str, JavaFileIndex] = {}


def _cache_dir() -> Path:
    return Path(get_settings().repo_storage_dir) / ".cache" / "parse"


def _index_from_json(d: dict[str, Any]) -> JavaFileIndex:
    syms = [Symbol(**{**s, "annotations": [Annotation(**a) for a in s["annotations"]]}) for s in d["symbols"]]
    return JavaFileIndex(
        package=d["package"],
        imports=d["imports"],
        symbols=syms,
        calls=[Call(**c) for c in d["calls"]],
        sync_blocks=[tuple(x) for x in d["sync_blocks"]],
    )  # type: ignore[misc]


def parse_cached(content: bytes, content_hash: str) -> tuple[JavaFileIndex, bool]:
    """按内容哈希缓存解析结果：内存一级 + 磁盘二级。返回 (结果, 是否命中缓存)。"""
    if content_hash in _parse_cache:
        return _parse_cache[content_hash], True
    fp = _cache_dir() / f"{content_hash}.json"
    if fp.exists():
        try:
            # 显式 utf-8：Windows 上默认编码是 GBK，写入时遇到 GBK 表示不了的字符会直接抛错
            idx = _index_from_json(json.loads(fp.read_text(encoding="utf-8")))
            _parse_cache[content_hash] = idx
            return idx, True
        except Exception:  # noqa: BLE001 —— 缓存坏了就重新解析
            pass
    idx = parse_java(content)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(dataclasses.asdict(idx), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    if len(_parse_cache) > 20000:
        _parse_cache.clear()
    _parse_cache[content_hash] = idx
    return idx, False


# ---------------------------------------------------------------- 扫描文件
def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def scan_files(root: Path, max_file_kb: int) -> list[tuple[str, bytes]]:
    """遍历快照目录，跳过 .git / node_modules / target / build / 生成代码 / 二进制 / 超大文件 / 敏感文件。"""
    out: list[tuple[str, bytes]] = []
    for p in sorted(root.rglob("*")):
        rel_parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]) or p.is_symlink() or not p.is_file():
            continue
        if is_sensitive(p.name):
            continue
        if p.stat().st_size > max_file_kb * 1024:
            continue
        data = p.read_bytes()
        if _is_binary(data):
            continue
        out.append(("/".join(rel_parts), data))
    return out


# ---------------------------------------------------------------- 主流程
async def run_analysis(snapshot_id: uuid.UUID, job_id: str | None = None) -> None:
    s = get_settings()
    job_id_var.set(job_id or str(snapshot_id))
    ctx = CallContext(node="analysis", job_id=job_id or f"analyze:{snapshot_id}")
    t_start = time.perf_counter()
    async with session_scope() as db:
        snap = await db.get(RepoSnapshot, snapshot_id)
        if snap is None:
            raise AnalysisError("快照不存在")
        repo = await db.get(Repository, snap.repository_id)
        assert repo is not None
        ctx.user_id = snap.requested_by
        # 找增量基线：同仓库最近一个 ready 的其他快照
        base = (
            await db.execute(
                select(RepoSnapshot)
                .where(
                    RepoSnapshot.repository_id == repo.id, RepoSnapshot.status == "ready", RepoSnapshot.id != snap.id
                )
                .order_by(RepoSnapshot.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        repo_url, sha, ref, repo_id = repo.url, snap.commit_sha, snap.ref, repo.id
        base_id = base.id if base else None
    try:
        # ---------------- 1. 克隆
        await set_status(snapshot_id, "cloning", 5, "正在浅克隆仓库")
        root = snapshot_root(repo_id, sha)
        if not root.exists():
            await git.shallow_fetch(repo_url, sha, root, timeout=s.clone_timeout_s, ref=ref)
        size, count = git.dir_size(root, {".git"})
        if size > s.max_repo_mb * 1024 * 1024:
            raise AnalysisError(f"仓库大小 {size // 1024 // 1024} MB 超过上限 {s.max_repo_mb} MB")
        if count > s.max_files:
            raise AnalysisError(f"文件数 {count} 超过上限 {s.max_files}")

        # ---------------- 2. 建索引
        await set_status(snapshot_id, "indexing", 20, "正在建立符号索引", size_bytes=size, file_count=count)
        sandbox = Sandbox(root, snapshot_id, ctx=ctx)
        files = await asyncio.to_thread(scan_files, root, s.max_file_kb)
        lang_stats: Counter[str] = Counter()
        sources: dict[str, str] = {}
        entries: list[FileEntry] = []
        file_rows: list[dict[str, Any]] = []
        build_files: list[TextFile] = []
        config_files: list[TextFile] = []
        cache_hits = 0
        for path, data in files:
            ext = Path(path).suffix.lower()
            lang = LANG_BY_EXT.get(ext)
            if lang:
                lang_stats[lang] += 1
            h = hashlib.sha256(data).hexdigest()
            text = data.decode("utf-8", errors="replace")
            fid = uuid.uuid4()
            file_rows.append(
                {
                    "id": fid,
                    "snapshot_id": snapshot_id,
                    "path": path,
                    "language": lang,
                    "size": len(data),
                    "content_hash": h,
                    "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
                }
            )
            name = Path(path).name
            if name == "pom.xml" or name.startswith("build.gradle"):
                build_files.append(TextFile(path, text))
            elif name.startswith(("application", "bootstrap")) and ext in (".yml", ".yaml", ".properties"):
                config_files.append(TextFile(path, text))
            if ext == ".java":
                # CPU 密集的解析放到线程池（SPEC 5.3）
                idx, hit = await asyncio.to_thread(parse_cached, data, h)
                cache_hits += int(hit)
                entries.append(FileEntry(id=fid, path=path, index=idx))
                sources[path] = text
        model = await asyncio.to_thread(CodeModel, entries)
        await _persist_index(snapshot_id, file_rows, model)

        # ---------------- 3. 画像
        await set_status(snapshot_id, "profiling", 45, "正在生成仓库画像")
        profile = build_profile(model, build_files, config_files, sources, dict(lang_stats))
        summary = profile_summary_md(profile)
        async with session_scope() as db:
            await db.execute(delete(RepoProfile).where(RepoProfile.snapshot_id == snapshot_id))
            db.add(RepoProfile(snapshot_id=snapshot_id, profile=profile, summary_md=summary))
            await db.commit()

        # ---------------- 4. 考点与题库
        await set_status(snapshot_id, "generating", 60, "正在生成考点和题库")
        stats = await _generate(snapshot_id, base_id, sandbox, model, profile, summary, file_rows, ctx)
        stats.update(
            {
                "java_files": len(entries),
                "parse_cache_hits": cache_hits,
                "loc": sum(r["lines"] for r in file_rows if r["language"] == "java"),
            }
        )
        if s.retrieval_mode in ("vector", "hybrid"):
            await set_status(snapshot_id, "generating", 85, "正在建立代码向量索引")
            stats.update(await build_code_vectors(snapshot_id, base_id, sandbox, model, file_rows, ctx))
        stats["elapsed_ms"] = int((time.perf_counter() - t_start) * 1000)
        await set_status(
            snapshot_id, "ready", 100, "分析完成", lang_stats=dict(lang_stats), stats=stats, base_snapshot_id=base_id
        )
    except Exception as e:
        logger.exception("analysis failed")
        msg = getattr(e, "message", None) or str(e) or type(e).__name__
        await set_status(snapshot_id, "failed", 100, "分析失败", error=msg[:2000])
        raise


async def _persist_index(snapshot_id: uuid.UUID, file_rows: list[dict[str, Any]], model: CodeModel) -> None:
    """一次性批量写入。重试时先清掉这个快照的旧索引，保证幂等。"""
    sym_rows = [
        {
            "id": s.id,
            "snapshot_id": snapshot_id,
            "file_id": s.file.id,
            "kind": s.sym.kind,
            "name": s.sym.name[:300],
            "qualified_name": s.sym.qualified_name[:1000],
            "signature": s.sym.signature[:2000],
            "start_line": s.sym.start_line,
            "end_line": s.sym.end_line,
            "annotations": [dataclasses.asdict(a) for a in s.sym.annotations] or None,
            "parent_id": s.owner.id if s.owner else None,
        }
        for s in model.symbols
    ]
    ref_rows = [
        {
            "id": uuid.uuid4(),
            "snapshot_id": snapshot_id,
            "from_symbol_id": rc.caller.id,
            "to_name": rc.call.name[:300],
            "receiver": (rc.call.receiver or None) and rc.call.receiver[:300],
            "to_symbol_id": rc.targets[0].id if rc.targets else None,
            "line": rc.call.line,
        }
        for rc in model.calls
    ]
    async with session_scope() as db:
        for tbl in (CodeChunk, CodeRef, CodeSymbol, CodeFile):
            await db.execute(delete(tbl).where(tbl.snapshot_id == snapshot_id))
        for tbl, rows in ((CodeFile, file_rows), (CodeSymbol, sym_rows), (CodeRef, ref_rows)):
            for i in range(0, len(rows), 2000):
                await db.execute(insert(tbl), rows[i : i + 2000])
        await db.commit()


async def _generate(
    snapshot_id: uuid.UUID,
    base_id: uuid.UUID | None,
    sandbox: Sandbox,
    model: CodeModel,
    profile: dict[str, Any],
    summary: str,
    file_rows: list[dict[str, Any]],
    ctx: CallContext,
) -> dict[str, Any]:
    candidates = build_candidates(model, profile)
    stats: dict[str, Any] = {
        "candidates": len(candidates),
        "topics_reused": 0,
        "questions_reused": 0,
        "tokens_saved": 0,
        "files_changed": len(file_rows),
        "files_total": len(file_rows),
    }
    reused_keys: set[str] = set()
    async with session_scope() as db:
        await db.execute(delete(Question).where(Question.snapshot_id == snapshot_id, Question.scope.is_(None)))
        await db.execute(delete(Topic).where(Topic.snapshot_id == snapshot_id))
        await db.commit()
        if base_id is not None:
            # ---- 增量：比对内容哈希
            old = dict(
                (
                    await db.execute(
                        select(CodeFile.path, CodeFile.content_hash).where(CodeFile.snapshot_id == base_id)
                    )
                ).all()
            )
            new = {r["path"]: r["content_hash"] for r in file_rows}
            changed = {p for p, h in new.items() if old.get(p) != h}
            stats["files_changed"] = len(changed)
            cand_keys = {c["topic_key"] for c in candidates}
            old_topics = (await db.execute(select(Topic).where(Topic.snapshot_id == base_id))).scalars().all()
            for t in old_topics:
                files = {e["path"] for e in t.evidence}
                if files & changed or not files <= set(new) or (t.topic_key not in cand_keys and t.kind != "design"):
                    continue
                nt = Topic(
                    snapshot_id=snapshot_id,
                    kind=t.kind,
                    title=t.title,
                    description=t.description,
                    evidence=t.evidence,
                    importance=t.importance,
                    topic_key=t.topic_key,
                )
                db.add(nt)
                await db.flush()
                reused_keys.add(t.topic_key)
                stats["topics_reused"] += 1
                qs = (
                    (
                        await db.execute(
                            select(Question).where(
                                Question.snapshot_id == base_id, Question.topic_id == t.id, Question.scope.is_(None)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for q in qs:
                    db.add(
                        Question(
                            snapshot_id=snapshot_id,
                            topic_id=nt.id,
                            mode=q.mode,
                            difficulty=q.difficulty,
                            text=q.text,
                            key_points=q.key_points,
                            followup_hints=q.followup_hints,
                            reference_md=q.reference_md,
                            topic_key=q.topic_key,
                            model=q.model,
                            tokens=0,
                        )
                    )
                    stats["questions_reused"] += 1
                    stats["tokens_saved"] += q.tokens
            await db.commit()
    todo = [c for c in candidates if c["topic_key"] not in reused_keys]
    tokens_used = 0
    topics: list[dict[str, Any]] = []
    if todo:
        topics, t_tokens = await generate_topics(sandbox, todo, summary, ctx.child("topics"))
        tokens_used += t_tokens
    async with session_scope() as db:
        topic_rows: list[tuple[Topic, dict[str, Any]]] = []
        for t in topics:
            if t["topic_key"] in reused_keys:
                continue
            row = Topic(
                snapshot_id=snapshot_id,
                kind=t["kind"],
                title=t["title"],
                description=t["description"],
                evidence=t["evidence"],
                importance=t["importance"],
                topic_key=t["topic_key"],
            )
            db.add(row)
            topic_rows.append((row, t))
        await db.commit()
    stats["topics"] = len(topic_rows) + stats["topics_reused"]
    # 题库：按重要度取前 N 个考点出题
    topic_rows.sort(key=lambda x: -x[1]["importance"])
    n_q = 0
    for i, (row, t) in enumerate(topic_rows[: max(0, MAX_TOPICS_WITH_QUESTIONS - stats["topics_reused"])]):
        qs, q_tokens, model_name = await generate_questions(
            sandbox, t, ctx.child("questions"), count=QUESTIONS_PER_TOPIC
        )
        tokens_used += q_tokens
        async with session_scope() as db:
            for q in qs:
                db.add(
                    Question(
                        snapshot_id=snapshot_id,
                        topic_id=row.id,
                        mode="deep_dive",
                        difficulty=q["difficulty"],
                        text=q["text"],
                        key_points=q["key_points"],
                        followup_hints=q["followup_hints"],
                        reference_md=q["reference_md"],
                        topic_key=t["topic_key"],
                        model=model_name,
                        tokens=q_tokens // max(1, len(qs)),
                    )
                )
                n_q += 1
            await db.commit()
        await set_status(
            snapshot_id, "generating", 60 + int(25 * (i + 1) / max(1, len(topic_rows))), f"已出题 {n_q} 道"
        )
    stats["questions"] = n_q + stats["questions_reused"]
    stats["tokens_used"] = tokens_used
    return stats


async def build_code_vectors(
    snapshot_id: uuid.UUID,
    base_id: uuid.UUID | None,
    sandbox: Sandbox,
    model: CodeModel,
    file_rows: list[dict[str, Any]],
    ctx: CallContext,
) -> dict[str, Any]:
    """按方法切块（超长再切）→ 嵌入 → 存 pgvector。旧快照里 (路径, 内容哈希) 相同的块直接复用向量。"""
    file_ids = {r["path"]: r["id"] for r in file_rows}
    chunks: list[dict[str, Any]] = []
    for s in model.symbols:
        if s.sym.kind not in ("method", "constructor"):
            continue
        lines = sandbox.read_lines_raw(s.path, s.sym.start_line, s.sym.end_line) or []
        for off in range(0, max(1, len(lines)), 60):
            part = lines[off : off + 80]
            if not part:
                continue
            text = "\n".join(part)
            chunks.append(
                {
                    "path": s.path,
                    "file_id": file_ids.get(s.path),
                    "symbol_id": s.id,
                    "start_line": s.sym.start_line + off,
                    "end_line": s.sym.start_line + off + len(part) - 1,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "text": f"{s.short}\n{text}",
                }
            )
    reuse: dict[tuple[str, str], list[float]] = {}
    if base_id is not None:
        async with session_scope() as db:
            for path, h, emb in (
                await db.execute(
                    select(CodeChunk.path, CodeChunk.content_hash, CodeChunk.embedding).where(
                        CodeChunk.snapshot_id == base_id
                    )
                )
            ).all():
                if emb is not None:
                    reuse[(path, h)] = list(emb)
    todo = [c for c in chunks if (c["path"], c["content_hash"]) not in reuse]
    vecs = await get_llm().embed([c["text"][:6000] for c in todo], ctx.child("embedding"))
    for c, v in zip(todo, vecs, strict=True):
        c["embedding"] = v
    for c in chunks:
        c.setdefault("embedding", reuse.get((c["path"], c["content_hash"])))
    rows = [
        {"id": uuid.uuid4(), "snapshot_id": snapshot_id, **{k: v for k, v in c.items() if k != "text"}}
        for c in chunks
        if c["file_id"]
    ]
    async with session_scope() as db:
        await db.execute(delete(CodeChunk).where(CodeChunk.snapshot_id == snapshot_id))
        for i in range(0, len(rows), 500):
            await db.execute(insert(CodeChunk), rows[i : i + 500])
        await db.commit()
    return {"chunks": len(rows), "chunks_reused": len(chunks) - len(todo)}
