"""假模型（LLM_PROVIDER=fake）。

每个任务一个确定性实现，只看调用方传入的 payload。原则：
- 输出必须符合对应的 Pydantic 结构，这样 fake 能测到真实的校验与落库路径；
- 行为要"有逻辑"：关键词命中多就判对，命中少就追问，这样图里的每个分支都能被测到；
- 永远不依赖网络和 Key（SPEC 18.2：测试不能依赖真实 API Key）。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.core.text import code_like, identifiers, tokenize
from app.llm.types import ToolCall

Handler = Callable[[dict[str, Any]], dict[str, Any] | str]
_HANDLERS: dict[str, Handler] = {}

UNKNOWN_WORDS = ("不知道", "不会", "不清楚", "没了解", "忘了", "pass", "跳过")
NEGATIONS = ("没有用", "没用到", "没有使用", "不是用", "并没有", "不使用", "没有")


def task(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[name] = fn
        return fn

    return deco


def handle(name: str, payload: dict[str, Any]) -> dict[str, Any] | str:
    fn = _HANDLERS.get(name)
    if fn is None:
        return {} if not name.endswith(".text") else ""
    return fn(payload)


# ---------------------------------------------------------------- 工具函数
def _kw_hit(keywords: list[str], answer: str) -> float:
    """要点关键词在回答里的命中比例（大小写不敏感，标识符也按驼峰拆开比）。"""
    if not keywords:
        return 0.0
    ans = answer.lower()
    ans_tokens = set(tokenize(answer))
    hit = 0
    for k in keywords:
        k_low = k.lower().lstrip("@")
        if k_low in ans or (set(tokenize(k)) and set(tokenize(k)) <= ans_tokens):
            hit += 1
    return hit / len(keywords)


def _snippet_idents(text: str, limit: int = 6) -> list[str]:
    """挑出片段里"像代码"的标识符，优先注解和方法调用。"""
    ids = identifiers(text)
    ann = [i for i in ids if i.startswith("@")]
    calls = [m.group(1) for m in re.finditer(r"\.?([a-zA-Z_][A-Za-z0-9_]{2,})\s*\(", text)]
    rest = [i for i in ids if code_like(i) and not i.startswith("@")]
    seen: dict[str, None] = {}
    for x in ann + calls + rest:
        if x.lower() in {"if", "for", "while", "switch", "catch", "return", "super", "this", "equals", "tostring"}:
            continue
        seen.setdefault(x, None)
    return list(seen)[:limit]


# ---------------------------------------------------------------- 考点
@task("topics.generate")
def _topics(p: dict[str, Any]) -> dict[str, Any]:
    # 假模型直接采用确定性抽取出的候选考点（真实模型会筛选、改写、补充说明）
    topics = []
    for c in p.get("candidates", [])[: p.get("limit", 30)]:
        topics.append(
            {
                "kind": c["kind"],
                "title": c["title"],
                "description": c.get("description") or c["title"],
                "evidence": c["evidence"][:3],
                "importance": c.get("importance", 3),
                "topic_key": c["topic_key"],
                "identifiers": c.get("identifiers", [])[:3],
            }
        )
    return {"topics": topics}


# ---------------------------------------------------------------- 出题
@task("questions.generate")
def _questions(p: dict[str, Any]) -> dict[str, Any]:
    topic = p["topic"]
    snippets: list[dict[str, Any]] = p.get("snippets", [])
    mode = p.get("mode", "deep_dive")
    kb = p.get("kb", [])
    points: list[dict[str, Any]] = []
    for sn in snippets[:4]:
        ids = _snippet_idents(sn["text"]) or topic.get("identifiers", []) or [topic["title"]]
        ev = [{"path": sn["path"], "start_line": sn["start_line"], "end_line": sn["end_line"]}]
        # 一个片段拆成最多两个要点：前半标识符一个、后半一个，保证至少 2 个要点
        halves = [ids[: max(1, len(ids) // 2)], ids[max(1, len(ids) // 2) :]] if len(ids) > 1 else [ids]
        for h in halves:
            if not h or len(points) >= 4:
                continue
            points.append(
                {
                    "point": f"在 {sn['path']} 中，{'、'.join(h[:3])} 承担了「{topic['title']}」的关键职责",
                    "evidence": ev,
                    "keywords": h[:3],
                }
            )
    if len(points) < 2:
        ev = topic["evidence"][:1] or [{"path": "README.md", "start_line": 1, "end_line": 1}]
        base = topic.get("identifiers") or [topic["title"]]
        points = [
            {"point": f"说明 {base[0]} 的作用", "evidence": ev, "keywords": base[:1]},
            {
                "point": f"说明「{topic['title']}」这样设计的原因与代价",
                "evidence": ev,
                "keywords": tokenize(topic["title"])[:2] or base[:1],
            },
        ]
    first = points[0]["keywords"][0] if points[0]["keywords"] else topic["title"]
    if mode == "kb_link" and kb:
        text = f"你的笔记里讲过「{kb[0].get('heading_path') or kb[0].get('title', '相关机制')}」。结合本项目的代码，说说「{topic['title']}」是怎么落地的？"
    elif mode == "chain":
        text = f"沿着链路走到这一站：「{topic['title']}」。这一站做了什么？数据从哪里来、交给了谁？"
    elif mode == "change":
        text = f"这次改动涉及「{topic['title']}」。说说改动的意图，以及它可能带来的副作用和需要补的测试。"
    elif mode == "resume":
        text = f"你的简历里写了「{topic['title']}」。请结合代码说明具体是怎么实现的。"
    else:
        text = f"请结合代码讲讲「{topic['title']}」：它是怎么实现的？为什么这样设计？"
    hints = [
        f"如果 {first} 执行失败了，会发生什么？",
        "这个设计在高并发下有什么隐患？",
        "如果让你重新设计，会怎么改？代价是什么？",
    ]
    ref = "\n".join(
        f"- {kp['point']}（`{kp['evidence'][0]['path']}:{kp['evidence'][0]['start_line']}`）" for kp in points
    )
    q = {
        "text": text,
        "difficulty": p.get("difficulty", "medium"),
        "key_points": points[:5],
        "followup_hints": hints,
        "reference_md": f"**参考要点**\n\n{ref}",
    }
    return {"questions": [q]}


@task("session.ask.text")
def _ask(p: dict[str, Any]) -> str:
    prefix = p.get("prefix", "")
    return f"{prefix}{p['text']}"


# ---------------------------------------------------------------- 面试官判断
@task("session.quick_assess")
def _assess(p: dict[str, Any]) -> dict[str, Any]:
    answer: str = p.get("answer", "")
    kps: list[dict[str, Any]] = p.get("key_points", [])
    claims = [i for i in identifiers(answer) if code_like(i)][:5]
    if not answer.strip() or any(w in answer for w in UNKNOWN_WORDS) and len(answer) < 40:
        return {
            "category": "unknown",
            "missing_points": list(range(len(kps))),
            "code_claims": [],
            "reasoning": "用户表示不会",
        }
    ratios = [_kw_hit(kp.get("keywords", []), answer) for kp in kps]
    missing = [i for i, r in enumerate(ratios) if r < 0.5]
    if not missing:
        cat = "complete"
    elif len(missing) < len(kps):
        cat = "partial"
    else:
        q_overlap = set(tokenize(p.get("question", ""))) & set(tokenize(answer))
        cat = "wrong" if q_overlap else "off_topic"
    return {"category": cat, "missing_points": missing, "code_claims": claims, "reasoning": f"命中率 {ratios}"}


@task("session.follow_up.text")
def _follow_up(p: dict[str, Any]) -> str:
    hints: list[str] = p.get("followup_hints") or ["能再具体一点吗？"]
    depth = int(p.get("depth", 0))
    parts = []
    cat = p.get("category")
    if cat == "partial":
        parts.append("你答到了一部分，但还不完整。")
    elif cat in ("wrong", "off_topic"):
        parts.append("这个回答和代码里的实现对不上。")
    for note in p.get("verify_notes", [])[:1]:
        parts.append(note)
    parts.append(hints[depth % len(hints)])
    return "".join(parts)


@task("hint.generate")
def _hint(p: dict[str, Any]) -> dict[str, Any]:
    level = int(p.get("level", 1))
    kps = p.get("key_points", [])
    attempt = int(p.get("attempt", 0))
    if p.get("next_station"):
        st = p["next_station"]
        return {"hint": f"下一站在 `{st['path']}` 的 `{st.get('name', '')}`，从那里往下看。"}
    if level == 1:
        h = "先别急着说细节，想想这个功能的数据最后是怎么落地的，中间经过了哪几层？"
        if attempt:
            h = "从入口方法开始，顺着调用一层层往下想。"
        return {"hint": h}
    ev = kps[0]["evidence"][0] if kps else {"path": "?", "start_line": 1, "end_line": 1}
    if level == 2:
        return {"hint": f"去看看 `{ev['path']}` 第 {ev['start_line']}–{ev['end_line']} 行附近。"}
    kw = kps[0].get("keywords", ["关键方法"])[0] if kps else "关键方法"
    return {"hint": f"其中一个关键点和 `{kw}` 有关，想想它被调用的时机。剩下的部分请你自己补全。"}


# ---------------------------------------------------------------- 阅卷
@task("grade.judge")
def _grade(p: dict[str, Any]) -> dict[str, Any]:
    answer = "\n".join(p.get("answers", []))
    kps = p.get("key_points", [])
    results = []
    total = 0.0
    for i, kp in enumerate(kps):
        r = _kw_hit(kp.get("keywords", []), answer)
        verdict = "hit" if r >= 0.5 else ("partial" if r > 0 else "miss")
        total += {"hit": 1.0, "partial": 0.5, "miss": 0.0}[verdict]
        results.append({"index": i, "verdict": verdict, "reason": f"关键词命中率 {r:.0%}"})
    contradictions = []
    for kp in kps:
        for k in kp.get("keywords", []):
            for neg in NEGATIONS:
                if f"{neg}{k}".lower() in answer.lower().replace(" ", ""):
                    contradictions.append(
                        {"claim": f"{neg}{k}", "evidence": kp["evidence"][:1], "explanation": f"代码中明确用到了 {k}"}
                    )
                    break
    score = int(round(100 * total / max(1, len(kps))))
    if contradictions:
        score = max(0, score - 20)
    if any(w in answer for w in UNKNOWN_WORDS) and len(answer) < 40:
        score = 0
    missed = [kps[r["index"]]["point"] for r in results if r["verdict"] != "hit"]
    fb = "回答覆盖了全部要点。" if not missed else "还需要补充：" + "；".join(missed[:3])
    return {
        "points": results,
        "contradictions": contradictions,
        "score": score,
        "feedback": fb,
        "review_topics": [p.get("topic_key", "")] if missed and p.get("topic_key") else [],
    }


# ---------------------------------------------------------------- 简历
@task("resume.extract")
def _resume_extract(p: dict[str, Any]) -> dict[str, Any]:
    text: str = p.get("text", "")
    projects: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        is_bullet = bool(re.match(r"^([-*•·]|\d+[.、)])\s*", line))
        head = re.match(r"^(#+\s*|【)?(.{2,40}?)(】)?\s*$", line)
        if (
            not is_bullet
            and (line.startswith("#") or line.startswith("【") or "项目" in line[:12])
            and head
            and len(line) < 50
        ):
            name = re.sub(r"^[#【\s]+|[】\s]+$", "", line).replace("项目名称：", "").replace("项目：", "")
            cur = {"name": name, "claims": []}
            projects.append(cur)
            continue
        if is_bullet and cur is not None:
            claim = re.sub(r"^([-*•·]|\d+[.、)])\s*", "", line)
            if re.search(r"\d+(\.\d+)?\s*(%|倍|ms|QPS|qps|TPS|万|w|s\b)", claim):
                ctype = "metric"
            elif re.search(r"(提升|优化|降低|减少|提高|解决|保证|避免)", claim):
                ctype = "effect"
            else:
                ctype = "tech"
            ents = [e.lstrip("@") for e in identifiers(claim)]
            cur["claims"].append({"text": claim, "type": ctype, "entities": ents[:6]})
    return {"projects": [pr for pr in projects if pr["claims"]]}


@task("resume.plan_queries")
def _resume_plan(p: dict[str, Any]) -> dict[str, Any]:
    ents = p.get("entities") or [w for w in tokenize(p.get("claim", "")) if re.match(r"[a-z]", w)][:3]
    if not ents:
        ents = tokenize(p.get("claim", ""))[:2] or [p.get("claim", "")[:10]]
    return {"queries": ents[:6]}


ALTERNATIVES = [
    {"kafka", "rabbitmq", "rocketmq"},
    {"mysql", "postgresql", "oracle"},
    {"redisson", "zookeeper"},
    {"mybatis", "jpa", "hibernate"},
]


@task("resume.verdict")
def _resume_verdict(p: dict[str, Any]) -> dict[str, Any]:
    hits: dict[str, int] = p.get("hits", {})
    ev = p.get("evidence", [])[:3]
    found = [k for k, v in hits.items() if v > 0]
    missing = [k for k, v in hits.items() if v == 0]
    present = {t.lower() for t in p.get("present_terms", [])}
    for m in missing:
        for group in ALTERNATIVES:
            if m.lower() in group and (group - {m.lower()}) & present:
                alt = next(iter((group - {m.lower()}) & present))
                return {
                    "verdict": "contradicted",
                    "evidence": ev,
                    "explanation": f"简历写的是 {m}，但代码里用的是 {alt}",
                    "suggestion": f"把 {m} 改成 {alt}，或补上相应实现",
                }
    if hits and not missing:
        return {
            "verdict": "supported",
            "evidence": ev,
            "explanation": f"在代码中找到了 {'、'.join(found)}",
            "suggestion": "说法有代码支撑，面试时准备好讲清实现细节",
        }
    if found:
        return {
            "verdict": "partial",
            "evidence": ev,
            "explanation": f"找到了 {'、'.join(found)}，没找到 {'、'.join(missing)}",
            "suggestion": f"补充 {'、'.join(missing)} 的实现位置，或修改措辞",
        }
    return {
        "verdict": "not_found",
        "evidence": [],
        "explanation": "代码中没有找到相关实现",
        "suggestion": "删掉这条，或换成代码里真实存在的内容",
    }


# ---------------------------------------------------------------- agentic 检索
def tool_step(task_name: str, payload: dict[str, Any], tool_names: list[str]) -> tuple[list[ToolCall], str]:
    """假模型的工具调用策略：第一轮按关键词 grep + find_symbol，第二轮读最相关的文件，第三轮结束。"""
    rnd = int(payload.get("round", 0))
    query: str = payload.get("query", "")
    if rnd == 0:
        ids = [i.lstrip("@") for i in identifiers(query)]
        words = ids or [w for w in tokenize(query) if len(w) >= 2][:3]
        calls: list[ToolCall] = []
        for i, w in enumerate(words[:3]):
            if "grep" in tool_names:
                calls.append(ToolCall(id=f"g{i}", name="grep", args={"pattern": re.escape(w)}))
            if "find_symbol" in tool_names and w[:1].isupper():
                calls.append(ToolCall(id=f"s{i}", name="find_symbol", args={"name": w}))
        return calls[:8], ""
    if rnd == 1 and payload.get("top_file") and "read_file" in tool_names:
        tf = payload["top_file"]
        start = max(1, int(tf.get("line", 1)) - 10)
        return [ToolCall(id="r0", name="read_file", args={"path": tf["path"], "start": start, "end": start + 40})], ""
    return [], "信息已足够"


# ---------------------------------------------------------------- 嵌入
def embed(text: str) -> list[float]:
    """哈希技巧（feature hashing）：词 → 哈希桶，词频作为权重，最后 L2 归一化。

    虽然不是语义向量，但字面重叠多的文本余弦相似度高，足够让向量检索的流程在测试里跑通。
    """
    dim = get_settings().embedding_dim
    vec = [0.0] * dim
    for tok in tokenize(text):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    if not any(vec):
        vec[0] = 1.0  # 全零向量的余弦距离是 NaN，给个默认方向
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@task("baseline.judge")
def _baseline(p: dict[str, Any]) -> dict[str, Any]:
    """基线的假实现：只看回答和检索到的代码有多少词重叠，不知道答案要点。"""
    from app.core.text import containment

    score = int(round(100 * containment(p.get("answer", ""), p.get("code", ""))))
    return {"points": [], "contradictions": [], "score": max(0, min(100, score)), "feedback": "基线判分", "review_topics": []}
