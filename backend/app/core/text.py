"""文本小工具：分词、标识符抽取、相似度。

BM25、提示防泄露、假模型都要用到"把中英混排文本切成词"，统一放这里。
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

import jieba

jieba.setLogLevel(logging.WARNING)

_IDENT = re.compile(r"@?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_")
STOPWORDS = {
    "的",
    "了",
    "是",
    "在",
    "和",
    "与",
    "或",
    "就",
    "都",
    "而",
    "及",
    "也",
    "这",
    "那",
    "一个",
    "什么",
    "怎么",
    "为什么",
    "如何",
    "我",
    "你",
    "他",
    "它",
    "们",
    "吗",
    "呢",
    "吧",
    "啊",
    "把",
    "被",
    "让",
    "给",
    "对",
    "从",
    "到",
    "用",
    "会",
    "能",
    "要",
    "有",
    "没有",
    "不",
    "很",
    "这个",
    "那个",
    "里",
    "中",
    "上",
    "下",
    "说",
    "讲",
    "请",
    "结合",
    "代码",
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "is",
    "and",
    "or",
    "for",
    "on",
    "with",
    "as",
    "by",
    "it",
    "this",
    "that",
    "public",
    "private",
    "protected",
    "static",
    "final",
    "void",
    "return",
    "new",
    "class",
    "import",
    "package",
    "string",
    "int",
    "long",
    "boolean",
    "if",
    "else",
    "null",
    "true",
    "false",
}


def identifiers(text: str) -> list[str]:
    """抽出代码标识符（含 @注解 和 a.b 形式），保持出现顺序去重。"""
    seen: dict[str, None] = {}
    for m in _IDENT.finditer(text):
        tok = m.group(0)
        if len(tok.lstrip("@")) < 3 or tok.lower() in STOPWORDS:
            continue
        seen.setdefault(tok, None)
    return list(seen)


def code_like(tok: str) -> bool:
    """像不像代码标识符：驼峰、带下划线、注解、带点号。普通英文单词不算。"""
    return bool(
        tok.startswith("@")
        or "." in tok
        or "_" in tok
        or re.search(r"[a-z][A-Z]", tok)
        or re.match(r"^[A-Z][a-z]+[A-Z]", tok)
    )


def tokenize(text: str) -> list[str]:
    """中英混合分词：英文标识符再按驼峰拆开，都转小写；去停用词和标点。"""
    out: list[str] = []
    for w in jieba.lcut(text):
        w = w.strip()
        if not w or re.fullmatch(r"[\W_]+", w):
            continue
        if re.fullmatch(r"[A-Za-z0-9_@.]+", w):
            w = w.lstrip("@")
            parts = [p for p in re.split(r"[._]", w) if p]
            for p in parts:
                subs = [s.lower() for s in _CAMEL_SPLIT.split(p) if s]
                out.append(p.lower())
                if len(subs) > 1:
                    out.extend(subs)
        else:
            out.append(w.lower())
    return [t for t in out if t not in STOPWORDS and len(t) > 1]


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokenize(a)), set(tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def cosine_tokens(a: str, b: str) -> float:
    ca, cb = Counter(tokenize(a)), Counter(tokenize(b))
    if not ca or not cb:
        return 0.0
    dot = sum(ca[k] * cb[k] for k in ca)
    return dot / (math.sqrt(sum(v * v for v in ca.values())) * math.sqrt(sum(v * v for v in cb.values())))


def containment(needle: str, haystack: str) -> float:
    """needle 的词有多大比例出现在 haystack 里。用来判断"提示是不是把要点说出来了"。"""
    n = set(tokenize(needle))
    if not n:
        return 0.0
    h = set(tokenize(haystack))
    return len(n & h) / len(n)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，原长 {len(text)} 字符]"
