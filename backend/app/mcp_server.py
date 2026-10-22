"""MCP Server（SPEC 9.6）：在 Claude Code、Cursor 等客户端里完成"分析仓库 → 考核 → 看报告"。

独立进程，stdio 通信；用设置页生成的个人 API 令牌调用后端接口（不直接连数据库，
这样权限、配额、限流和网页端完全一致）。

运行：PB_API_URL=http://localhost:8090/api PB_API_TOKEN=pbt_xxx uv run python -m app.mcp_server

注：SPEC 写的是 FastMCP；mcp 2.x 把它改名为 MCPServer（见 D-012），用法相同。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

API_URL = os.environ.get("PB_API_URL", "http://localhost:8090/api").rstrip("/")
API_TOKEN = os.environ.get("PB_API_TOKEN", "")

mcp = MCPServer(
    "project-b",
    instructions="代码理解考核：AI 当考官，以代码为证据考你对一个仓库的理解。先 analyze_repo，再 start_session，"
    "然后用 answer 作答、hint 要提示，结束后 get_report 看报告。",
)

_transport: httpx.AsyncBaseTransport | None = None  # 测试时注入 ASGITransport


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API_URL, headers={"Authorization": f"Bearer {API_TOKEN}"}, timeout=120,
                             transport=_transport)


async def _get(path: str, **params: Any) -> Any:
    async with _client() as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict[str, Any] | None = None) -> Any:
    async with _client() as c:
        r = await c.post(path, json=body or {})
        if r.status_code >= 400:
            raise RuntimeError(r.json().get("error", {}).get("message", r.text))
        return r.json()


async def _stream(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """读取 SSE 流，把面试官的文字拼起来，同时收集工具调用和状态。"""
    text: list[str] = []
    tools: list[str] = []
    out: dict[str, Any] = {}
    async with _client() as c, c.stream("POST", path, json=body or {}) as r:
        event = None
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                data = json.loads(line[6:])
                if event == "token":
                    text.append(data["text"])
                elif event == "tool":
                    tools.append(data.get("message", ""))
                elif event in ("next_question", "followup"):
                    text.append(f"\n\n[{'新题' if event == 'next_question' else '追问'}] ")
                elif event == "hint":
                    text.append(f"[提示 {data['level']} 级，扣 {data['penalty']} 分] {data['text']}")
                elif event == "session_finished":
                    out["finished"] = data["reason"]
                elif event == "error":
                    out["error"] = data["message"]
                elif event == "done":
                    out["state"] = data
    out["text"] = "".join(text).strip()
    if tools:
        out["examiner_checked"] = tools
    return out


@mcp.tool()
async def list_repos() -> list[dict[str, Any]]:
    """列出我提交过的仓库和最新快照状态。"""
    repos = await _get("/repos")
    return [{"repository_id": r["id"], "url": r["url"], "snapshot_id": (r.get("latest_snapshot") or {}).get("id"),
             "status": (r.get("latest_snapshot") or {}).get("status")} for r in repos]


@mcp.tool()
async def analyze_repo(url: str, ref: str | None = None, wait_seconds: int = 600) -> dict[str, Any]:
    """提交一个公开仓库（可指定分支、标签或 SHA）并等待分析完成。同一提交已分析过会直接复用。"""
    res = await _post("/repos", {"url": url, "ref": ref})
    sid = res["snapshot_id"]
    deadline = asyncio.get_running_loop().time() + wait_seconds
    snap = await _get(f"/snapshots/{sid}")
    while snap["status"] not in ("ready", "failed") and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(3)
        snap = await _get(f"/snapshots/{sid}")
    out = {"snapshot_id": sid, "status": snap["status"], "reused": res["reused"], "error": snap.get("error")}
    if snap["status"] == "ready":
        prof = await _get(f"/snapshots/{sid}/profile")
        out["summary"] = prof["summary_md"][:3000]
    return out


@mcp.tool()
async def start_session(snapshot: str, mode: str = "deep_dive", n: int = 5, endpoint: str | None = None) -> dict[str, Any]:
    """开始一场考核。mode：deep_dive 项目深挖 / kb_link 八股联系项目 / chain 链路学习（需 endpoint）/ resume / change。"""
    cfg = {"endpoint": endpoint} if endpoint else {}
    res = await _post("/sessions", {"snapshot_id": snapshot, "mode": mode, "question_count": n, "config": cfg})
    first = next((t["content"] for t in reversed(res["turns"]) if t["role"] == "examiner"), "")
    return {"session_id": res["session"]["id"], "question": first, "state": res["state"]}


@mcp.tool()
async def answer(session_id: str, text: str) -> dict[str, Any]:
    """回答当前问题。返回面试官的追问或下一题。"""
    return await _stream(f"/sessions/{session_id}/answer", {"text": text})


@mcp.tool()
async def hint(session_id: str) -> dict[str, Any]:
    """要一个提示（一级指方向、二级指位置、三级给部分解释；用了会按级别扣分）。"""
    return await _stream(f"/sessions/{session_id}/hint")


@mcp.tool()
async def get_report(session_id: str, wait_seconds: int = 300) -> str:
    """获取考核报告（Markdown）。会话还在进行中时先结束它；阅卷是异步的，会等待报告生成。"""
    sess = await _get(f"/sessions/{session_id}")
    if sess["session"]["status"] == "active":
        await _post(f"/sessions/{session_id}/finish")
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        rep = await _get(f"/sessions/{session_id}/report")
        if rep.get("ready"):
            return str(rep["summary_md"])
        await asyncio.sleep(2)
    return "报告还在生成中，请稍后再调用 get_report。"


def main() -> None:
    if not API_TOKEN:
        raise SystemExit("请设置 PB_API_TOKEN（在网页的设置页生成个人 API 令牌）")
    mcp.run("stdio")


if __name__ == "__main__":
    main()
