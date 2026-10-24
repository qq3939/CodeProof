import httpx
from langchain_core.tools import tool

from app.rag import retriever


@tool
def search_docs(query: str) -> str:
    """在知识库里检索和问题相关的片段。"""
    hits = retriever.search(query, top_k=3)
    return "\n".join(h["text"] for h in hits)


@tool
def fetch_url(url: str) -> str:
    """抓取一个网页的正文。"""
    # 没有设超时：对方不响应时整个 Agent 会一直卡在这里
    return httpx.get(url).text


TOOLS = [search_docs, fetch_url]
TOOL_MAP = {t.name: t for t in TOOLS}
