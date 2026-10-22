"""三种检索模式的统一入口（SPEC 8.2）。

出题、面试官核对、简历核验都调用 retrieve()，评测时只改 RETRIEVAL_MODE 就能对比三种模式。

- agentic：模型自己决定关键词，循环调用 grep / glob / read_file / find_symbol，每轮最多 8 次工具调用；
- vector：问题嵌入成向量，取最相近的代码块；
- hybrid：两路结果按 RRF 融合。

返回统一的 EvidenceHit 列表（文件 + 行范围 + 片段），上层不关心是怎么找到的。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import get_settings
from app.llm.client import get_llm, wrap_data
from app.llm.types import CallContext
from app.retrieval.rrf import rrf
from app.tools.sandbox import Sandbox

Mode = Literal["agentic", "vector", "hybrid"]
MAX_ROUNDS = 3
MAX_CALLS_PER_ROUND = 8
_HIT = re.compile(r"^([^:\n]+?):(\d+):(.*)$")
_READ = re.compile(r"^(\S+) 第 (\d+)-(\d+) 行")
_SYM = re.compile(r" @ (\S+?):(\d+)-(\d+)")


@dataclass
class EvidenceHit:
    path: str
    start_line: int
    end_line: int
    text: str = ""
    score: float = 0.0
    source: str = "agentic"


@dataclass
class RetrievalResult:
    hits: list[EvidenceHit]
    tool_calls: int = 0
    latency_ms: int = 0
    mode: str = "agentic"
    transcript: list[str] = field(default_factory=list)


AGENTIC_SYSTEM = (
    "你是代码检索助手。目标：为给定问题在仓库中找到最相关的代码证据（文件和行号）。"
    "用 grep、glob、find_symbol 定位，再用 read_file 读取关键片段。每轮最多调用 8 个工具。"
    "信息足够时，不再调用工具，直接用一句话说明找到了什么。"
)


def _parse_locations(tool: str, output: str) -> list[tuple[str, int, int]]:
    locs: list[tuple[str, int, int]] = []
    if tool == "grep":
        for line in output.splitlines():
            m = _HIT.match(line)
            if m:
                ln = int(m.group(2))
                locs.append((m.group(1), ln, ln))
    elif tool == "read_file":
        m = _READ.match(output)
        if m:
            locs.append((m.group(1), int(m.group(2)), int(m.group(3))))
    elif tool in ("find_symbol", "find_references"):
        for m in _SYM.finditer(output):
            locs.append((m.group(1), int(m.group(2)), int(m.group(3))))
        for line in output.splitlines():
            m2 = re.match(r"^(\S+?):(\d+) 在 ", line)
            if m2:
                ln = int(m2.group(2))
                locs.append((m2.group(1), ln, ln))
    return locs


def _merge(locs: list[tuple[str, int, int]], sandbox: Sandbox, top_k: int, window: int = 6) -> list[EvidenceHit]:
    """把零散的命中行合并成证据窗口：同一文件、相近行合并，按命中次数排序。"""
    counter: Counter[tuple[str, int]] = Counter()
    spans: dict[tuple[str, int], tuple[int, int]] = {}
    for path, s, e in locs:
        bucket = (path, s // 30)
        counter[bucket] += 1
        lo, hi = spans.get(bucket, (s, e))
        spans[bucket] = (min(lo, s), max(hi, e))
    hits: list[EvidenceHit] = []
    for bucket, cnt in counter.most_common(top_k):
        path = bucket[0]
        lo, hi = spans[bucket]
        lines_all = sandbox.read_text_raw(path)
        if lines_all is None:
            continue
        n = len(lines_all.splitlines())
        start, end = max(1, lo - window), min(n, max(hi, lo) + window)
        if end - start > 60:
            end = start + 60
        text = "\n".join(lines_all.splitlines()[start - 1 : end])
        hits.append(EvidenceHit(path=path, start_line=start, end_line=end, text=text, score=float(cnt)))
    return hits


async def agentic(
    query: str, sandbox: Sandbox, ctx: CallContext, top_k: int = 5, max_rounds: int = MAX_ROUNDS
) -> RetrievalResult:
    llm = get_llm()
    t0 = time.perf_counter()
    messages: list[object] = [
        {"role": "system", "content": AGENTIC_SYSTEM},
        {"role": "user", "content": wrap_data("question", query)},
    ]
    locs: list[tuple[str, int, int]] = []
    calls = 0
    transcript: list[str] = []
    top_file: dict[str, object] | None = None
    for rnd in range(max_rounds):
        step = await llm.tool_step(
            task="retrieval.agentic",
            tier="fast",
            messages=messages,
            tools=sandbox.specs(),
            ctx=ctx,
            payload={"query": query, "round": rnd, "top_file": top_file},
        )
        if not step.tool_calls:
            break
        results: list[tuple[str, str]] = []
        for tc in step.tool_calls[:MAX_CALLS_PER_ROUND]:
            out = await sandbox.call(tc.name, tc.args, node=ctx.node)
            calls += 1
            transcript.append(f"{tc.name}({tc.args})")
            locs.extend(_parse_locations(tc.name, out))
            results.append((tc.id, out))
        if locs and top_file is None:
            path, line, _ = Counter((p, s // 30 * 30 + 1, 0) for p, s, _ in locs).most_common(1)[0][0]
            top_file = {"path": path, "line": line}
        messages.extend(llm.tool_messages(step, results))
    hits = _merge(locs, sandbox, top_k)
    return RetrievalResult(
        hits=hits,
        tool_calls=calls,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        mode="agentic",
        transcript=transcript,
    )


async def vector(query: str, sandbox: Sandbox, ctx: CallContext, top_k: int = 5) -> RetrievalResult:
    from app.retrieval.vector import vector_search

    t0 = time.perf_counter()
    if sandbox.snapshot_id is None:
        return RetrievalResult(hits=[], mode="vector")
    vh = await vector_search(sandbox.snapshot_id, query, top_k=top_k)
    hits = []
    for h in vh:
        lines = sandbox.read_lines_raw(h.path, h.start_line, h.end_line)
        if lines is not None:
            hits.append(
                EvidenceHit(
                    path=h.path,
                    start_line=h.start_line,
                    end_line=h.end_line,
                    text="\n".join(lines),
                    score=h.score,
                    source="vector",
                )
            )
    return RetrievalResult(hits=hits, tool_calls=1, latency_ms=int((time.perf_counter() - t0) * 1000), mode="vector")


async def retrieve(
    query: str, sandbox: Sandbox, ctx: CallContext, *, mode: Mode | None = None, top_k: int = 5
) -> RetrievalResult:
    mode = mode or get_settings().retrieval_mode
    if mode == "agentic":
        return await agentic(query, sandbox, ctx, top_k)
    if mode == "vector":
        return await vector(query, sandbox, ctx, top_k)
    a = await agentic(query, sandbox, ctx, top_k * 2)
    v = await vector(query, sandbox, ctx, top_k * 2)
    key = {(h.path, h.start_line // 30): h for h in a.hits + v.hits}
    fused = rrf(
        [[(h.path, h.start_line // 30) for h in a.hits], [(h.path, h.start_line // 30) for h in v.hits]], top_n=top_k
    )
    hits = []
    for k, score in fused:
        h = key[k]
        hits.append(
            EvidenceHit(
                path=h.path, start_line=h.start_line, end_line=h.end_line, text=h.text, score=score, source="hybrid"
            )
        )
    return RetrievalResult(
        hits=hits,
        tool_calls=a.tool_calls + v.tool_calls,
        latency_ms=a.latency_ms + v.latency_ms,
        mode="hybrid",
        transcript=a.transcript,
    )
