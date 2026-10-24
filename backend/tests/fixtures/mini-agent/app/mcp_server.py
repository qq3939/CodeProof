from mcp.server.fastmcp import FastMCP

from app.rag.retriever import answer_with_citations

mcp = FastMCP("mini-agent")


@mcp.tool()
def ask_docs(question: str) -> str:
    """在知识库里检索并回答，结果带引用。"""
    return answer_with_citations(question)


@mcp.resource("docs://index")
def list_docs() -> str:
    return "docs index"


if __name__ == "__main__":
    mcp.run(transport="stdio")
