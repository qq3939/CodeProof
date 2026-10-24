from openai import OpenAI
from sqlalchemy import text

from app.db import SessionLocal

CHUNK_SIZE = 500
OVERLAP = 50
_client = OpenAI(timeout=20)


def split_text(doc: str) -> list[str]:
    # 相邻块重叠 OVERLAP 个字，避免一句话被切在两块中间、两边都检索不到
    chunks = []
    start = 0
    while start < len(doc):
        chunks.append(doc[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - OVERLAP
    return chunks


def embed(texts: list[str]) -> list[list[float]]:
    resp = _client.embeddings.create(model="text-embedding-3-small", input=texts)
    return [d.embedding for d in resp.data]


def ingest(db, title: str, doc: str) -> int:
    chunks = split_text(doc)
    for chunk, vec in zip(chunks, embed(chunks)):
        db.execute(
            text("INSERT INTO chunks(title, text, embedding) VALUES (:t, :x, :e)"),
            {"t": title, "x": chunk, "e": vec},
        )
    db.commit()
    return len(chunks)


def search(query: str, top_k: int = 5) -> list[dict]:
    [qv] = embed([query])
    with SessionLocal() as db:
        rows = db.execute(
            text("SELECT title, text, embedding <=> :q AS dist FROM chunks ORDER BY embedding <=> :q LIMIT :k"),
            {"q": qv, "k": top_k},
        ).all()
    # 距离太远的不要，宁可回答"不知道"也不要拿不相关的片段硬答
    return [{"title": r.title, "text": r.text} for r in rows if r.dist < 0.5]


def answer_with_citations(query: str) -> str:
    hits = search(query)
    if not hits:
        return "知识库里没有找到相关内容。"
    return "\n".join(f"[{i + 1}] {h['title']}" for i, h in enumerate(hits))
