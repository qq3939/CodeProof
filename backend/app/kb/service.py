"""知识库 RAG（SPEC 9.3）：Markdown 切块 → BM25 + 向量 → RRF 融合。

为什么要混合检索：
- BM25 擅长精确词（"REQUIRES_NEW"、"ZSet"），向量擅长同义改写（"事务失效" ≈ "注解不生效"）；
- 两路各取前 20，用 RRF 按名次融合，不用纠结两种分数怎么归一化。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from rank_bm25 import BM25Okapi
from sqlalchemy import select, update

from app.core.db import session_scope
from app.core.text import tokenize
from app.llm.client import get_llm
from app.llm.types import CallContext
from app.models import KbChunk, KbDocument
from app.retrieval.rrf import rrf

CHUNK_SIZE = 800
OVERLAP = 100


def split_markdown(text: str) -> list[tuple[str, str]]:
    """按二、三级标题切成段，再把长段按约 800 字切块、相邻块重叠 100 字。返回 [(标题路径, 内容)]。"""
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = []
    buf: list[str] = []
    title = ""
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            if buf and "".join(buf).strip():
                sections.append((list(path), buf))
            buf = []
            level, head = len(m.group(1)), m.group(2).strip()
            if level == 1:
                title, path = head, []
            elif level == 2:
                path = [head]
            else:
                path = path[:1] + [head]
            continue
        buf.append(line)
    if buf and "".join(buf).strip():
        sections.append((list(path), buf))
    out: list[tuple[str, str]] = []
    for p, lines in sections:
        body = "\n".join(lines).strip()
        heading = " > ".join(([title] if title else []) + p)
        if len(body) <= CHUNK_SIZE:
            out.append((heading, body))
            continue
        start = 0
        while start < len(body):
            out.append((heading, body[start : start + CHUNK_SIZE]))
            if start + CHUNK_SIZE >= len(body):
                break
            start += CHUNK_SIZE - OVERLAP
    return out


async def add_document(user_id: uuid.UUID, title: str, filename: str | None, text: str) -> KbDocument:
    chunks = split_markdown(text)
    async with session_scope() as db:
        doc = KbDocument(user_id=user_id, title=title, filename=filename, chunk_count=len(chunks))
        db.add(doc)
        await db.flush()
        for heading, content in chunks:
            db.add(
                KbChunk(
                    document_id=doc.id,
                    user_id=user_id,
                    heading_path=heading,
                    content=content,
                    tokens=tokenize(heading + " " + content),
                )
            )
        await db.commit()
        await db.refresh(doc)
    return doc


async def embed_document(document_id: uuid.UUID) -> None:
    async with session_scope() as db:
        doc = await db.get(KbDocument, document_id)
        if doc is None:
            return
        rows = (
            await db.execute(
                select(KbChunk.id, KbChunk.heading_path, KbChunk.content).where(KbChunk.document_id == document_id)
            )
        ).all()
    vecs = await get_llm().embed([f"{h}\n{c}" for _, h, c in rows], CallContext(node="kb_embed", user_id=doc.user_id))
    async with session_scope() as db:
        for (cid, _, _), v in zip(rows, vecs, strict=True):
            await db.execute(update(KbChunk).where(KbChunk.id == cid).values(embedding=v))
        await db.commit()


async def search(user_id: uuid.UUID, query: str, top_k: int = 5) -> list[dict[str, Any]]:
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(KbChunk.id, KbChunk.tokens, KbChunk.heading_path, KbChunk.content, KbDocument.title)
                .join(KbDocument, KbDocument.id == KbChunk.document_id)
                .where(KbChunk.user_id == user_id)
            )
        ).all()
        if not rows:
            return []
        info = {r.id: r for r in rows}
        # BM25：按用户把自己的块全部载入内存建索引。个人笔记一般几百到几千块，毫秒级
        q_tokens = tokenize(query)
        bm25_rank: list[uuid.UUID] = []
        if q_tokens:
            bm = BM25Okapi([r.tokens or ["_"] for r in rows])
            scores = bm.get_scores(q_tokens)
            qset = set(q_tokens)
            # 小语料下 BM25 的 IDF 可能为 0 甚至负数，所以用"是否有词重叠"过滤，而不是 score > 0
            overlap = [len(qset & set(r.tokens or [])) for r in rows]
            order = sorted((i for i in range(len(rows)) if overlap[i]), key=lambda i: (-scores[i], -overlap[i]))
            bm25_rank = [rows[i].id for i in order[:20]]
        [qv] = await get_llm().embed([query])
        dist = KbChunk.embedding.cosine_distance(qv)
        vec_rank = [
            r[0]
            for r in (
                await db.execute(
                    select(KbChunk.id)
                    .where(KbChunk.user_id == user_id, KbChunk.embedding.is_not(None))
                    .order_by(dist)
                    .limit(20)
                )
            ).all()
        ]
    fused = rrf([bm25_rank, vec_rank], top_n=top_k)
    out = []
    for cid, score in fused:
        r = info[cid]
        out.append(
            {
                "chunk_id": str(cid),
                "title": r.title,
                "heading_path": r.heading_path,
                "content": r.content,
                "score": round(score, 5),
                "bm25_rank": bm25_rank.index(cid) + 1 if cid in bm25_rank else None,
                "vector_rank": vec_rank.index(cid) + 1 if cid in vec_rank else None,
            }
        )
    return out
