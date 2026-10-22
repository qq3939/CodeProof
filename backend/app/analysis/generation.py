"""考点生成（快模型）和题目生成（强模型）。SPEC 7.5、7.6。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.schemas import QuestionList, QuestionOut, TopicList, TopicOut
from app.analysis import evidence as evcheck
from app.core.text import truncate
from app.llm.client import get_llm, wrap_data
from app.llm.types import CallContext, LLMError
from app.tools.sandbox import Sandbox

logger = logging.getLogger(__name__)

TOPIC_SYSTEM = """你是资深 Java 面试官，正在为一个仓库挑选考点。
输入是确定性分析得出的候选考点（每个都带真实代码证据）和仓库画像摘要。
任务：
1. 从候选中挑出最值得考的点，可以改写标题和说明，让它像真实面试会问的问题方向；
2. 可以补充"设计决策"类考点，但证据必须来自给出的片段，行号必须真实；
3. 每个考点给出类型（design 设计决策 / risk 风险点 / chain 链路 / mechanism 机制）、重要度 1-5、topic_key；
4. topic_key 尽量沿用候选里的值，便于跨版本追踪掌握度；
5. identifiers 填证据片段里真实出现的类名或方法名。"""

QUESTION_SYSTEM = """你是资深 Java 面试官，根据一个考点和它的代码证据出面试题。
要求：
- 题目必须基于代码事实，像真实面试官会问的，不要问八股定义；
- 每道题 2-5 个答案要点，每个要点都要带证据位置（文件 + 行号，必须落在给出的片段范围内）；
- keywords 填答对该要点时通常会提到的词（类名、方法名、关键概念）；
- followup_hints 是追问方向，不能直接包含答案；
- reference_md 是给考生复习用的参考讲解。"""


def _snippet_block(sandbox: Sandbox, evs: list[dict[str, Any]], limit_chars: int) -> tuple[str, list[dict[str, Any]]]:
    parts, out = [], []
    used = 0
    for ev in evs:
        text = evcheck.snippet(sandbox, ev)
        if text is None:
            continue
        numbered = "\n".join(f"{i}: {ln}" for i, ln in enumerate(text.splitlines(), int(ev["start_line"])))
        block = f"--- {ev['path']} 第 {ev['start_line']}-{ev['end_line']} 行\n{numbered}"
        if used + len(block) > limit_chars:
            break
        used += len(block)
        parts.append(block)
        out.append({**ev, "text": text})
    return "\n".join(parts), out


async def generate_topics(
    sandbox: Sandbox, candidates: list[dict[str, Any]], profile_md: str, ctx: CallContext, limit: int = 30
) -> tuple[list[dict[str, Any]], int]:
    """返回 (校验通过的考点, 消耗 Token)。模型失败时退回到确定性候选。"""
    if not candidates:
        return [], 0
    blocks = []
    budget = 14000
    for i, c in enumerate(candidates):
        text, _ = _snippet_block(sandbox, c["evidence"][:2], 1500)
        block = f"### 候选 {i}: [{c['kind']}] {c['title']} (topic_key={c['topic_key']})\n{c['description']}\n{text}"
        if budget - len(block) < 0:
            break
        budget -= len(block)
        blocks.append(block)
    user = f"{wrap_data('profile', truncate(profile_md, 4000))}\n\n{wrap_data('candidates', chr(10).join(blocks))}\n\n最多输出 {limit} 个考点。"
    llm = get_llm()
    tokens = 0
    try:
        out, usage = await llm.structured(
            task="topics.generate",
            tier="fast",
            system=TOPIC_SYSTEM,
            user=user,
            schema=TopicList,
            ctx=ctx,
            payload={"candidates": candidates, "limit": limit},
        )
        tokens += usage.total
        topics = out.topics
    except LLMError as e:
        logger.warning("topic generation failed, fallback to candidates: %s", e)
        topics = [TopicOut.model_validate({**c, "evidence": c["evidence"][:3]}) for c in candidates[:limit]]

    good: list[dict[str, Any]] = []
    bad: list[tuple[TopicOut, str]] = []
    for t in topics:
        d = t.model_dump()
        err = evcheck.check(sandbox, d["evidence"], d.get("identifiers", []), d["title"] + " " + d["description"])
        (bad.append((t, err)) if err else good.append(d))
    if bad:
        # 不通过的考点带上原因重新生成一次，还不通过就丢弃
        fix_user = (
            user
            + "\n\n下面这些考点的证据校验没通过，请只重新输出修正后的这些考点：\n"
            + "\n".join(f"- {t.title}: {err}" for t, err in bad)
        )
        try:
            out2, usage2 = await llm.structured(
                task="topics.generate",
                tier="fast",
                system=TOPIC_SYSTEM,
                user=fix_user,
                schema=TopicList,
                ctx=ctx,
                payload={"candidates": [], "limit": len(bad)},
            )
            tokens += usage2.total
            for t in out2.topics:
                d = t.model_dump()
                if not evcheck.check(sandbox, d["evidence"], d.get("identifiers", []), d["title"]):
                    good.append(d)
        except LLMError:
            pass
        logger.info("topics dropped after evidence check", extra={"extra_data": {"bad": len(bad)}})
    # topic_key 去重
    seen, uniq = set(), []
    for d in good:
        if d["topic_key"] not in seen:
            seen.add(d["topic_key"])
            uniq.append(d)
    return uniq, tokens


def _validate_question(sandbox: Sandbox, q: QuestionOut) -> str | None:
    for i, kp in enumerate(q.key_points):
        evs = [e.model_dump() for e in kp.evidence]
        err = evcheck.check(sandbox, evs, [], "")
        if err:
            return f"要点 {i}: {err}"
    return None


async def generate_questions(
    sandbox: Sandbox,
    topic: dict[str, Any],
    ctx: CallContext,
    *,
    mode: str = "deep_dive",
    difficulty: str = "medium",
    count: int = 1,
    extra_snippets: list[dict[str, Any]] | None = None,
    kb: list[dict[str, Any]] | None = None,
    extra_context: str = "",
) -> tuple[list[dict[str, Any]], int, str]:
    """为一个考点出题。返回 (题目列表, Token, 模型名)。"""
    snippet_text, snippets = _snippet_block(sandbox, topic["evidence"] + (extra_snippets or []), 9000)
    kb_text = "\n\n".join(
        f"[{k.get('title')} / {k.get('heading_path')}]\n{k.get('content', '')[:800]}" for k in (kb or [])
    )
    user = (
        f"考点：[{topic['kind']}] {topic['title']}\n说明：{topic.get('description', '')}\n"
        f"模式：{mode}，难度：{difficulty}，出 {count} 道题。\n{extra_context}\n\n"
        f"{wrap_data('code', snippet_text)}"
        + (f"\n\n{wrap_data('knowledge_base', kb_text)}\n八股部分要和上面的代码事实联系起来。" if kb_text else "")
    )
    payload = {"topic": topic, "snippets": snippets, "mode": mode, "difficulty": difficulty, "kb": kb or []}
    llm = get_llm()
    tokens = 0
    result: list[dict[str, Any]] = []
    for attempt in range(2):
        try:
            out, usage = await llm.structured(
                task="questions.generate",
                tier="strong",
                system=QUESTION_SYSTEM,
                user=user,
                schema=QuestionList,
                ctx=ctx,
                payload=payload,
            )
        except LLMError as e:
            logger.warning("question generation failed: %s", e)
            break
        tokens += usage.total
        errors = []
        for q in out.questions[:count]:
            err = _validate_question(sandbox, q)
            if err:
                errors.append(err)
            else:
                result.append(q.model_dump())
        if result or attempt == 1:
            break
        user += "\n\n上一次输出的证据校验没通过：" + json.dumps(errors, ensure_ascii=False) + "\n请修正行号后重新输出。"
    return result, tokens, llm.model_for("strong")
