"""改动理解模式（SPEC 9.5）：提交范围 base..head 或公开 PR。

1. PR 通过 git 拉取 refs/pull/<n>/head，不需要 API 令牌；base 取 PR 目标分支（默认分支）和 head 的 merge-base；
2. git diff 解析改动 → 找出受影响的符号（改动行落在方法范围内）和它们的调用方；
3. 围绕改动意图、副作用、边界情况和测试生成考点，之后走同样的考核和阅卷流程。

改动在单独的目录里检出（/data/repos/{repo}/changes/{session}），不碰共享快照目录。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.analysis.lang_index import CALLABLE_KINDS, parser_for
from app.core.config import get_settings
from app.core.db import session_scope
from app.models import CodeRef, CodeSymbol, ExamSession, Repository, RepoSnapshot
from app.repos import git
from app.repos.url import normalize_repo_url

logger = logging.getLogger(__name__)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: list[str] = field(default_factory=list)
    added: list[int] = field(default_factory=list)  # 新文件里的行号
    removed: list[int] = field(default_factory=list)  # 旧文件里的行号
    removed_at: list[int] = field(default_factory=list)  # 删除发生在新文件的哪一行附近


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added / deleted / modified / renamed
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_lines(self) -> list[int]:
        return [n for h in self.hunks for n in h.added]

    @property
    def changed_count(self) -> int:
        return sum(len(h.added) + len(h.removed) for h in self.hunks)


def parse_diff(text: str) -> list[FileDiff]:
    """解析 unified diff。只依赖 git diff 的标准输出格式，便于单测。"""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_ln = new_ln = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = FileDiff(path=m.group(2) if m else "", old_path=m.group(1) if m else None, status="modified")
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from "):
            cur.status, cur.old_path = "renamed", line[len("rename from ") :]
        elif line.startswith("rename to "):
            cur.path = line[len("rename to ") :]
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if m:
                hunk = Hunk(
                    int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1), m.group(5).strip()
                )
                cur.hunks.append(hunk)
                old_ln, new_ln = hunk.old_start, hunk.new_start
        elif hunk is not None:
            hunk.lines.append(line)
            if line.startswith("+"):
                hunk.added.append(new_ln)
                new_ln += 1
            elif line.startswith("-"):
                hunk.removed.append(old_ln)
                hunk.removed_at.append(new_ln)
                old_ln += 1
            elif not line.startswith("\\"):
                old_ln += 1
                new_ln += 1
    return files


def affected_symbols(fd: FileDiff, source: bytes) -> list[dict[str, Any]]:
    """改动行落在哪些方法（Python 还有函数）里。新增/修改的都算受影响。

    走和分析流水线同一个语言接口（lang_index，D-022）：v1 只解析 Java，Python 文件的改动找不到受影响的函数。
    """
    parser = parser_for(fd.path)
    if parser is None:
        return []
    idx = parser[1](source, fd.path)
    added = set(fd.added_lines)
    removed_at = {n for h in fd.hunks for n in h.removed_at}
    out = []
    for s in idx.symbols:
        if s.kind not in CALLABLE_KINDS:
            continue
        touched = any(s.start_line <= n <= s.end_line for n in added | removed_at)
        if touched:
            owner = idx.symbols[s.parent].name if s.parent is not None else ""
            out.append(
                {
                    "name": s.name,
                    "short": f"{owner}.{s.name}" if owner else s.name,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "signature": s.signature,
                }
            )
    return out


async def prepare_change(session_id: uuid.UUID, job_id: str | None = None) -> None:
    from app.sessions.service import start_graph

    s = get_settings()
    async with session_scope() as db:
        sess = await db.get(ExamSession, session_id)
        assert sess is not None
        snap = await db.get(RepoSnapshot, sess.snapshot_id)
        assert snap is not None
        repo = await db.get(Repository, snap.repository_id)
        assert repo is not None
        cfg = dict(sess.config or {})
    work = Path(s.repo_storage_dir) / str(repo.id) / "changes" / str(session_id)
    try:
        if work.exists():
            import shutil

            shutil.rmtree(work)
        work.parent.mkdir(parents=True, exist_ok=True)
        await git.run_git(["init", "-q", str(work)])
        await git.run_git(["remote", "add", "origin", repo.url], cwd=work)
        if cfg.get("pr_url"):
            ru = normalize_repo_url(cfg["pr_url"], allow_file=s.allow_file_repos)
            if ru.pr_number is None or ru.url != repo.url:
                raise git.GitError("PR 地址必须属于当前仓库，形如 https://github.com/owner/repo/pull/123")
            head = await git.fetch_commit(work, f"refs/pull/{ru.pr_number}/head", s.clone_timeout_s)
            target = await git.fetch_commit(work, repo.default_branch or "HEAD", s.clone_timeout_s)
            base = await git.merge_base(work, target, head) or target
            label = f"PR #{ru.pr_number}"
        else:
            head = await git.fetch_commit(work, cfg["head"], s.clone_timeout_s)
            base = await git.fetch_commit(work, cfg["base"], s.clone_timeout_s)
            label = f"{cfg['base'][:10]}..{cfg['head'][:10]}"
        await git.run_git(["checkout", "--force", head], cwd=work)
        diff_text = await git.diff(work, base, head)
        files = parse_diff(diff_text)
        if not files:
            raise git.GitError("这个提交范围里没有改动")
        topics = await _change_topics(files, work, snap.id, label, diff_text)
        cfg.update(
            {
                "change_dir": str(work),
                "change_topics": topics,
                "change_label": label,
                "base_sha": base,
                "head_sha": head,
                "changed_files": [{"path": f.path, "status": f.status, "lines": f.changed_count} for f in files][:200],
            }
        )
        async with session_scope() as db:
            row = await db.get(ExamSession, session_id)
            assert row is not None
            row.config = cfg
            row.status = "active"
            row.question_count = min(row.question_count, max(1, len(topics)))
            await db.commit()
        await start_graph(session_id)
    except Exception as e:
        logger.exception("change prepare failed")
        async with session_scope() as db:
            row = await db.get(ExamSession, session_id)
            assert row is not None
            row.status = "failed"
            row.finish_reason = (getattr(e, "message", None) or str(e))[:200]
            await db.commit()
        raise


async def _change_topics(
    files: list[FileDiff], work: Path, snapshot_id: uuid.UUID, label: str, diff_text: str
) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    biggest = sorted([f for f in files if f.status != "deleted" and f.hunks], key=lambda f: -f.changed_count)
    if biggest:
        f = biggest[0]
        h = f.hunks[0]
        p = work / f.path
        n_lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines()) if p.exists() else 1
        topics.append(
            {
                "kind": "change",
                "title": f"{label} 的改动意图",
                "importance": 5,
                "topic_key": "change:intent",
                "description": f"共改动 {len(files)} 个文件，主要集中在 {', '.join(x.path for x in biggest[:3])}",
                "evidence": [
                    {
                        "path": f.path,
                        "start_line": max(1, h.new_start),
                        "end_line": min(n_lines, max(h.new_start, h.new_start + max(h.new_len, 1) - 1)),
                    }
                ],
                "identifiers": [],
                "diff_excerpt": diff_text[:3000],
            }
        )
    async with session_scope() as db:
        for f in biggest:
            if parser_for(f.path) is None or not (work / f.path).exists():
                continue
            for sym in affected_symbols(f, (work / f.path).read_bytes())[:4]:
                callers = (
                    (
                        await db.execute(
                            select(CodeSymbol.qualified_name)
                            .join(CodeRef, CodeRef.from_symbol_id == CodeSymbol.id)
                            .where(CodeRef.snapshot_id == snapshot_id, CodeRef.to_name == sym["name"])
                            .limit(5)
                        )
                    )
                    .scalars()
                    .all()
                )
                hunk_text = "\n".join(line for h in f.hunks for line in h.lines)[:2000]
                topics.append(
                    {
                        "kind": "change",
                        "title": f"改动后的 {sym['short']}",
                        "importance": 4,
                        "topic_key": f"change:{sym['short']}",
                        "description": f"{sym['signature']} 被修改；调用方：{', '.join(callers) or '未找到'}。"
                        "说说改动带来的副作用、边界情况和应该补的测试。",
                        "evidence": [
                            {
                                "path": f.path,
                                "start_line": sym["start_line"],
                                "end_line": min(sym["end_line"], sym["start_line"] + 59),
                            }
                        ],
                        "identifiers": [sym["name"]],
                        "diff_excerpt": hunk_text,
                    }
                )
            if len(topics) >= 8:
                break
    return topics
