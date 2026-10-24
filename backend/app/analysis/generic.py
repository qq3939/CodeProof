"""通用兜底：没有专门画像的语言也要能出题（SPEC 7.4.4、D-021）。

Java 有符号索引和 Spring 画像，候选考点由规则从代码里抽出来；其他语言没有这些，
v1 的候选考点是空的，结果"分析完成"却一道题都出不了。这里改成让快模型看目录结构、
README、构建文件、几份主要源码和关键词搜索结果，自己挑候选考点。
模型给的候选考点之后照常交给 generate_topics：和规则考点走同一套证据校验，编出来的行号会被丢掉。
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Any

from app.agents.schemas import TopicList
from app.analysis import evidence as evcheck
from app.analysis.candidates import topic_key_slug
from app.analysis.languages import display_name, interviewer_title
from app.core.text import truncate
from app.llm.client import get_llm, wrap_data
from app.llm.types import CallContext, LLMError
from app.tools.sandbox import Sandbox

logger = logging.getLogger(__name__)

BUILD_FILE_NAMES = {
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "Gemfile",
    "composer.json",
    "CMakeLists.txt",
    "Makefile",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "Dockerfile",
    "docker-compose.yml",
}

# 和语言无关的关键词：这些地方通常有设计取舍或风险，给模型当线索
GREP_PATTERNS = {
    "重试与超时": r"(?i)\b(retry|retries|timeout|backoff)\b",
    "缓存": r"(?i)\b(cache|redis|memcached?)\b",
    "并发": r"(?i)\b(mutex|lock|semaphore|goroutine|thread|async|await|chan)\b",
    "事务": r"(?i)\b(transaction|commit|rollback)\b",
    "鉴权": r"(?i)\b(auth\w*|jwt|token|permission)\b",
    "消息与定时": r"(?i)\b(queue|kafka|rabbitmq|publish|subscribe|cron|schedule\w*)\b",
    "路由与入口": r"(?i)(\brouter\b|\broute\b|handlefunc|\bhandler\b|@app\.|app\.(get|post)\()",
}
HITS_PER_PATTERN = 12
MAX_SOURCE_FILES = 6
SOURCE_LINES = 150
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|_test\.|\.test\.|\.spec\.|(^|/)test_")

GENERIC_SYSTEM = """你是{title}，正在为一个{lang}仓库挑选考点。
这个语言没有专门的符号索引和框架画像，你只能看到目录结构、README、构建文件、几份主要源码（带行号）和关键词搜索结果（格式：路径:行号:内容）。
任务：
1. 挑出 8–15 个最值得考的点，类型只能是 design 设计决策 / risk 风险点 / chain 链路 / mechanism 机制，优先设计决策和风险点；
2. 每个考点的证据（文件 + 起止行号）只能取自给出的源码片段或搜索结果所在的行附近，范围不超过 40 行，行号必须真实；
3. identifiers 填证据范围里真实出现的函数名、类型名或关键调用；
4. topic_key 用小写英文和连字符概括考点，例如 cache-invalidation、retry-on-timeout；
5. 标题和说明写成真实面试会问的方向，不要编造代码里没有的东西。"""


def _is_test(path: str) -> bool:
    return bool(TEST_FILE.search(path))


def _tree(paths: list[str], limit: int = 300) -> str:
    shown = paths[:limit]
    more = f"\n…（共 {len(paths)} 个文件，只列前 {limit} 个）" if len(paths) > limit else ""
    return "\n".join(shown) + more


def _numbered(sandbox: Sandbox, path: str, max_lines: int) -> str | None:
    text = sandbox.read_text_raw(path)
    if text is None:
        return None
    lines = text.splitlines()[:max_lines]
    return f"--- {path} 第 1-{len(lines)} 行\n" + "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1))


async def _grep_hits(sandbox: Sandbox) -> list[dict[str, Any]]:
    """每个关键词取前几条命中；解析成 {path, line, text}，同一行只留一次。"""
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for label, pattern in GREP_PATTERNS.items():
        out = await sandbox.grep(pattern)
        n = 0
        for ln in out.splitlines():
            m = re.match(r"^(.+?):(\d+):(.*)$", ln)
            if not m or _is_test(m.group(1)):
                continue
            key = (m.group(1), int(m.group(2)))
            if key in seen:
                continue
            seen.add(key)
            hits.append({"label": label, "path": m.group(1), "line": int(m.group(2)), "text": m.group(3).strip()[:200]})
            n += 1
            if n >= HITS_PER_PATTERN:
                break
    return hits


async def generic_candidates(
    sandbox: Sandbox, file_rows: list[dict[str, Any]], lang: str | None, ctx: CallContext, limit: int = 15
) -> tuple[list[dict[str, Any]], int]:
    """返回 (候选考点, 消耗 Token)。格式和 candidates.build_candidates 的输出一样。"""
    paths = sorted(r["path"] for r in file_rows)
    if not paths:
        return [], 0
    readme = next((p for p in paths if PurePosixPath(p).name.lower().startswith("readme")), None)
    builds = [p for p in paths if PurePosixPath(p).name in BUILD_FILE_NAMES][:4]
    # 主语言里最大的几个非测试文件，通常是核心逻辑所在
    sources = sorted(
        (r for r in file_rows if r["language"] == lang and not _is_test(r["path"])), key=lambda r: -r["size"]
    )[:MAX_SOURCE_FILES]
    blocks = [wrap_data("tree", _tree(paths))]
    if readme:
        blocks.append(wrap_data("readme", truncate(sandbox.read_text_raw(readme) or "", 3000)))
    for p in builds:
        blocks.append(wrap_data(f"build:{p}", truncate(sandbox.read_text_raw(p) or "", 1500)))
    budget = 12000
    for r in sources:
        text = _numbered(sandbox, r["path"], SOURCE_LINES)
        if text is None or len(text) > budget:
            continue
        budget -= len(text)
        blocks.append(wrap_data(f"source:{r['path']}", text))
    hits = await _grep_hits(sandbox)
    blocks.append(wrap_data("grep", "\n".join(f"[{h['label']}] {h['path']}:{h['line']}:{h['text']}" for h in hits)))

    name = display_name(lang)
    system = GENERIC_SYSTEM.format(title=interviewer_title(lang), lang=f" {name} " if name else "")
    try:
        out, usage = await get_llm().structured(
            task="topics.generic",
            tier="fast",
            system=system,
            user="\n\n".join(blocks) + f"\n\n最多输出 {limit} 个考点。",
            schema=TopicList,
            ctx=ctx,
            payload={"hits": hits, "lang": lang, "limit": limit},
        )
    except LLMError as e:
        logger.warning("generic topics failed: %s", e)
        return [], 0
    cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in out.topics:
        d = t.model_dump()
        # 先粗筛一遍证据：行号不存在的直接丢，不进下一步的提示词
        if evcheck.check(sandbox, d["evidence"], d.get("identifiers", []), d["title"]):
            continue
        key = "generic:" + (topic_key_slug(d["topic_key"]) or topic_key_slug(d["title"]) or str(len(cands)))
        if key in seen:
            continue
        seen.add(key)
        cands.append({**d, "topic_key": key, "files": sorted({e["path"] for e in d["evidence"]})})
    return cands, usage.total


def generic_summary_md(lang: str | None, lang_stats: dict[str, int], n_files: int) -> str:
    """通用模式的画像摘要：只能如实说明"没有专门画像"，不能套用 Java 的统计口径。"""
    stats = "、".join(f"{k} {v}" for k, v in sorted(lang_stats.items(), key=lambda x: -x[1])[:8]) or "无"
    return "\n".join(
        [
            "## 通用模式",
            "",
            f"- 主语言：{display_name(lang) or '未识别'}",
            f"- 文件：共 {n_files} 个；按语言统计：{stats}",
            "- 这个语言没有专门的符号索引和框架画像，考点由模型根据目录结构、README、构建文件、"
            "主要源码和关键词搜索结果挑出，每个考点仍然校验证据行号。题目质量比有画像的语言低。",
        ]
    )
