"""面试官图里用到的模型调用：改写题干、快速评估、追问、提示（SPEC 8.3、8.5）。

图的编排在 sessions/graph.py，这里只放"怎么问模型"，方便单独测试和改提示词。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from app.agents.schemas import HintOut, QuickAssessOut
from app.analysis.languages import display_name
from app.core.config import get_settings
from app.core.text import containment
from app.llm.client import get_llm, wrap_data
from app.llm.types import CallContext, LLMError

logger = logging.getLogger(__name__)


def role(lang: str | None) -> str:
    """面试官的身份按仓库主语言填写（D-021）；v1 写死"Java 技术面试官"。

    题目字典里的 lang 来自出题计划（planner.build_plan）；没有时（比如改动前开始的会话）只说"技术面试官"。
    """
    name = display_name(lang)
    who = f" {name} 技术面试官" if name else "技术面试官"
    return (
        f"你是一位严谨但友善的{who}，正在以候选人自己的代码为证据考察他对代码的理解。"
        "你只提问、追问、给提示，绝不直接说出答案，也不替候选人写代码。"
    )


LEAK_THRESHOLD = 0.6


def _points_text(key_points: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{i}. {kp['point']}（证据 {kp['evidence'][0]['path']}:{kp['evidence'][0]['start_line']}）"
        for i, kp in enumerate(key_points)
    )


def leaks(text: str, key_points: list[dict[str, Any]], threshold: float = LEAK_THRESHOLD) -> bool:
    """防泄露：生成的文本和任何一个答案要点太相似就算泄露（SPEC 8.5）。

    用"要点里的词有多大比例出现在提示里"（containment）衡量，而不是 Jaccard：
    提示通常比要点短，Jaccard 会被长度差稀释，containment 更能反映"是不是把要点说出来了"。
    """
    return any(containment(kp["point"], text) >= threshold for kp in key_points if kp.get("point"))


async def ask_stream(question: dict[str, Any], style: str, ctx: CallContext, prefix: str = "") -> AsyncIterator[str]:
    """流式输出题干。style=plain 直接吐题库原文（零成本）；interviewer 让强模型改写成面试官口吻。"""
    if style != "interviewer":
        text = prefix + question["text"]
        for i in range(0, len(text), 12):
            yield text[i : i + 12]
        return
    system = f"{role(question.get('lang'))}\n把下面的题目改写成自然的面试官口吻，保持原意，不要增加提示，不要透露答案。只输出改写后的问题。"
    async for piece in get_llm().stream(
        task="session.ask.text",
        tier="strong",
        system=system,
        user=wrap_data("question", question["text"]),
        ctx=ctx.child("ask"),
        payload={"text": question["text"], "prefix": prefix},
    ):
        yield piece


async def quick_assess(question: dict[str, Any], answer: str, ctx: CallContext) -> QuickAssessOut:
    system = (
        f"{role(question.get('lang'))}\n判断候选人对这道题的回答属于哪一类：complete 完整 / partial 部分 / wrong 错误 / off_topic 跑题 / unknown 不会。"
        "列出还没答到的要点下标，并抽出回答里关于代码的具体说法（类名、方法名、注解、配置），供下一步去代码里核对。"
        "这只是用来决定下一步怎么问，不是最终分数。"
    )
    user = (
        f"题目：{question['text']}\n\n答案要点：\n{_points_text(question['key_points'])}\n\n"
        f"{wrap_data('candidate_answer', answer)}"
    )
    try:
        out, _ = await get_llm().structured(
            task="session.quick_assess",
            tier="fast",
            system=system,
            user=user,
            schema=QuickAssessOut,
            ctx=ctx.child("quick_assess"),
            payload={"question": question["text"], "key_points": question["key_points"], "answer": answer},
        )
        out.missing_points = [i for i in out.missing_points if 0 <= i < len(question["key_points"])]
        return out
    except LLMError as e:
        logger.warning("quick_assess failed: %s", e)
        return QuickAssessOut(category="partial", missing_points=list(range(len(question["key_points"]))))


async def follow_up(
    question: dict[str, Any],
    assess: QuickAssessOut,
    verify_notes: list[str],
    depth: int,
    ctx: CallContext,
    answers: list[str],
) -> str:
    """生成追问。先完整生成、做防泄露检查，再由调用方分片推给前端（见 D-008）。"""
    missing = [question["key_points"][i] for i in assess.missing_points] or question["key_points"]
    system = (
        f"{role(question.get('lang'))}\n候选人的回答有缺口。针对缺口提一个追问：可以引用代码位置（文件、方法名），"
        "但绝不能把答案要点直接说出来。只输出一句或两句追问。"
    )
    user = (
        f"题目：{question['text']}\n\n还没答到的要点（只给你看，不能透露）：\n{_points_text(missing)}\n\n"
        f"可参考的追问方向：{'; '.join(question.get('followup_hints', []))}\n\n"
        f"查代码核对的结果：{'; '.join(verify_notes) or '无'}\n\n"
        f"{wrap_data('candidate_answers', chr(10).join(answers))}"
    )
    payload = {
        "question": question["text"],
        "followup_hints": question.get("followup_hints", []),
        "depth": depth,
        "category": assess.category,
        "verify_notes": verify_notes,
    }
    llm = get_llm()
    for attempt in range(2):
        text = await llm.text(
            task="session.follow_up.text",
            tier="strong",
            system=system,
            user=user,
            ctx=ctx.child("follow_up"),
            payload=payload,
        )
        if not leaks(text, missing):
            return text.strip()
        user += "\n\n注意：上一次的追问把要点说出来了，请换一种只指方向的问法。"
        logger.info("follow-up leaked key point, regenerate", extra={"extra_data": {"attempt": attempt}})
    hints = question.get("followup_hints") or ["能结合代码再具体说说吗？"]
    return hints[depth % len(hints)]


HINT_LEVEL_DESC = {
    1: "一级提示：只指方向，例如“想想计数最后是怎么写回数据库的”，不提具体文件。",
    2: "二级提示：指出位置，告诉候选人该看哪个文件或方法，但不解释。",
    3: "三级提示：给出部分解释，但留一部分让候选人自己补全，绝不给完整答案。",
}
GENERIC_HINTS = {
    1: "换个角度想想：这个功能的数据最终落到了哪里？中间经过了哪几层？",
    2: "可以去看看题目相关的那个业务方法，从它的调用开始往下读。",
    3: "关键在于这里用到的那个中间件或注解，想想它解决了什么问题、代价是什么。",
}


async def give_hint(
    question: dict[str, Any],
    level: int,
    ctx: CallContext,
    answers: list[str],
    next_station: dict[str, Any] | None = None,
) -> str:
    """分级提示 + 防泄露（SPEC 8.5）：和要点太相似就重新生成，最多 3 次，再不行用通用模板。"""
    level = max(1, min(3, level))
    system = f"{role(question.get('lang'))}\n候选人请求提示。{HINT_LEVEL_DESC[level]} 只输出提示本身。"
    if next_station:
        system += f"\n这是链路学习模式，优先告诉候选人“下一站在哪”：{next_station.get('symbol')}。"
    user = (
        f"题目：{question['text']}\n\n答案要点（不能透露）：\n{_points_text(question['key_points'])}\n\n"
        f"{wrap_data('candidate_answers', chr(10).join(answers) or '（还没作答）')}"
    )
    llm = get_llm()
    for attempt in range(3):
        try:
            out, _ = await llm.structured(
                task="hint.generate",
                tier="fast",
                system=system,
                user=user,
                schema=HintOut,
                ctx=ctx.child("hint"),
                payload={
                    "level": level,
                    "key_points": question["key_points"],
                    "attempt": attempt,
                    "next_station": {"path": next_station["file"], "name": next_station["symbol"]}
                    if next_station
                    else None,
                },
            )
        except LLMError:
            break
        if not leaks(out.hint, question["key_points"]):
            return out.hint
        user += "\n\n上一次的提示太接近答案了，请更含蓄一些。"
    return GENERIC_HINTS[level]


def hint_penalty(level: int) -> int:
    pen = get_settings().hint_penalty
    if level <= 0:
        return 0
    return pen[min(level, len(pen)) - 1]
