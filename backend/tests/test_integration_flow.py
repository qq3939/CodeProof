"""集成测试（假模型 + 真 Postgres/Redis）：完整流程、并发去重、断点续答、配额、增量分析。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.core.queue import drain
from tests.conftest import commit_change, login, make_repo

pytestmark = pytest.mark.asyncio(loop_scope="session")


def parse_sse(text: str) -> list[tuple[str, Any]]:
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            out.append((ev, data))
    return out


async def post_stream(c: httpx.AsyncClient, url: str, body: dict[str, Any] | None = None) -> list[tuple[str, Any]]:
    r = await c.post(url, json=body or {})
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


def text_of(events: list[tuple[str, Any]]) -> str:
    return "".join(d["text"] for e, d in events if e == "token")


async def test_analysis_result(user_client: httpx.AsyncClient, analyzed: dict[str, Any]) -> None:
    sid = analyzed["snapshot_id"]
    snap = analyzed["snapshot"]
    assert snap["stats"]["topics"] > 0 and snap["stats"]["questions"] > 0
    prof = (await user_client.get(f"/api/snapshots/{sid}/profile")).json()
    assert len(prof["profile"]["endpoints"]) == 3
    assert "super-secret-password" not in json.dumps(prof)
    topics = (await user_client.get(f"/api/snapshots/{sid}/topics")).json()
    assert any(t["kind"] == "risk" for t in topics)
    tree = (await user_client.get(f"/api/snapshots/{sid}/tree", params={"path": "."})).json()
    names = [i["name"] for i in tree["items"]]
    assert "src" in names and ".env" not in names and ".git" not in names
    f = await user_client.get(f"/api/snapshots/{sid}/file", params={"path": ".env"})
    assert f.status_code == 404
    f = await user_client.get(f"/api/snapshots/{sid}/file", params={"path": "../../../etc/passwd"})
    assert f.status_code == 404
    # 再次提交同一个仓库：直接复用
    again = await user_client.post("/api/repos", json={"url": f"file://{analyzed['repo_path']}"})
    assert again.json()["reused"] is True and again.json()["snapshot_id"] == sid
    # SSE：已完成的快照直接推最终状态
    ev = await user_client.get(f"/api/jobs/{sid}/events")
    assert parse_sse(ev.text)[0][1]["status"] == "ready"


async def test_full_session_flow(user_client: httpx.AsyncClient, analyzed: dict[str, Any]) -> None:
    sid = analyzed["snapshot_id"]
    r = await user_client.post(
        "/api/sessions", json={"snapshot_id": sid, "mode": "deep_dive", "question_count": 3, "max_depth": 2}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    session_id = data["session"]["id"]
    assert data["turns"][0]["role"] == "examiner" and data["state"]["awaiting_answer"]
    assert any(e["event"] == "next_question" for e in data["events"])

    # 1) 不完整的回答 → 追问；回答里提到不存在的类 → 面试官去查代码
    ev = await post_stream(user_client, f"/api/sessions/{session_id}/answer", {"text": "我觉得是用 FooBarManager 做的"})
    kinds = [e for e, _ in ev]
    assert "tool" in kinds, kinds
    assert any("面试官正在查看" in d["message"] for e, d in ev if e == "tool")
    assert "followup" in kinds
    assert ev[-1][0] == "done" and ev[-1][1]["depth"] == 1

    # 2) 提示：分级、扣分
    ev = await post_stream(user_client, f"/api/sessions/{session_id}/hint")
    hint = next(d for e, d in ev if e == "hint")
    assert hint["level"] == 1 and hint["penalty"] == 5
    ev = await post_stream(user_client, f"/api/sessions/{session_id}/hint")
    assert next(d for e, d in ev if e == "hint")["level"] == 2

    # 3) 用答案要点里的关键词作答 → 判为完整 → 下一题
    from app.core.db import session_scope
    from app.models import Question

    qid = ev[-1][1]["question_id"]
    async with session_scope() as db:
        q = await db.get(Question, __import__("uuid").UUID(qid))
        kw = " ".join(k for kp in q.key_points for k in kp["keywords"])
    ev = await post_stream(user_client, f"/api/sessions/{session_id}/answer", {"text": f"关键在于 {kw}"})
    assert "next_question" in [e for e, _ in ev] and text_of(ev)

    # 4) 跳过第二题，第三题直接结束
    r = await user_client.post(f"/api/sessions/{session_id}/skip")
    assert r.status_code == 200 and any(e["event"] == "next_question" for e in r.json()["events"])
    r = await user_client.post(f"/api/sessions/{session_id}/finish")
    assert any(e["event"] == "session_finished" for e in r.json()["events"])
    await drain()

    rep = (await user_client.get(f"/api/sessions/{session_id}/report")).json()
    assert rep["ready"], rep
    items = rep["items"]
    assert len(items) == 3
    first = items[0]
    assert first["hint_level"] == 2 and first["score"] == first["raw_score"] - 10
    assert items[1]["skipped"] and items[1]["score"] == 0
    # 报告里的证据行号都能点开（文件接口能返回对应行）
    for it in items:
        for kp in it["key_points"]:
            ev0 = kp["evidence"][0]
            fr = await user_client.get(
                f"/api/snapshots/{sid}/file",
                params={"path": ev0["path"], "start": ev0["start_line"], "end": ev0["end_line"]},
            )
            assert fr.status_code == 200 and fr.json()["total_lines"] >= ev0["end_line"]
    md = await user_client.get(f"/api/sessions/{session_id}/report.md")
    assert md.status_code == 200 and "# 考核报告" in md.text
    mastery = (await user_client.get("/api/mastery", params={"repository_id": analyzed["repository_id"]})).json()
    assert len(mastery) >= 3
    sess = (await user_client.get(f"/api/sessions/{session_id}")).json()["session"]
    assert sess["status"] == "finished" and sess["total_tokens"] > 0 and sess["cost"] > 0


async def test_session_resumes_after_restart(user_client: httpx.AsyncClient, analyzed: dict[str, Any]) -> None:
    from app.sessions.graph import close_checkpointer

    r = await user_client.post("/api/sessions", json={"snapshot_id": analyzed["snapshot_id"], "question_count": 2})
    session_id = r.json()["session"]["id"]
    # 模拟 api 重启：丢掉内存里的图和连接池，之后只能从 Postgres 检查点恢复
    await close_checkpointer()
    detail = (await user_client.get(f"/api/sessions/{session_id}")).json()
    assert detail["state"]["awaiting_answer"] and detail["state"]["index"] == 1
    ev = await post_stream(user_client, f"/api/sessions/{session_id}/answer", {"text": "不知道"})
    assert ev[-1][0] == "done" and ev[-1][1]["index"] == 2  # 说不会 → 不追问，直接下一题
    await user_client.post(f"/api/sessions/{session_id}/finish")
    await drain()


async def test_checkpoint_setup_concurrent(app_ready: None) -> None:
    # 夹具里让两个 setup_checkpointer() 在全新数据库上同时跑（模拟 api 和 worker 一起启动，P-013）。
    # 没有排队时两边的 CREATE INDEX CONCURRENTLY 会死锁，被中断的索引会留成 INVALID
    from sqlalchemy import text

    from app.core.db import get_engine

    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                    " WHERE c.relname LIKE 'checkpoint%'"
                )
            )
        ).all()
    names = {r[0] for r in rows}
    assert {"checkpoints_thread_id_idx", "checkpoint_writes_thread_id_idx"} <= names
    assert all(valid for _, valid in rows)


async def test_concurrent_submit_creates_one_snapshot(app_ready: None) -> None:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.models import Job, RepoSnapshot

    repo = make_repo("concurrent-repo")
    clients = [await login(None, f"u{i}") for i in range(5)]
    rs = await asyncio.gather(*[c.post("/api/repos", json={"url": f"file://{repo}"}) for c in clients])
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs]
    assert len({r.json()["snapshot_id"] for r in rs}) == 1
    assert sum(1 for r in rs if not r.json()["reused"]) == 1
    async with session_scope() as db:
        n = (
            await db.execute(
                select(func.count()).select_from(RepoSnapshot).where(RepoSnapshot.id == rs[0].json()["snapshot_id"])
            )
        ).scalar_one()
        jobs = (
            await db.execute(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "analyze", Job.args["snapshot_id"].astext == rs[0].json()["snapshot_id"])
            )
        ).scalar_one()
    assert n == 1 and jobs == 1
    await drain()
    for c in clients:
        await c.aclose()


async def test_quota_exceeded(app_ready: None, analyzed: dict[str, Any]) -> None:
    from app.core.redis import get_redis
    from app.llm.accounting import quota_key

    c = await login(None, "poor")
    me = (await c.get("/api/me")).json()
    await get_redis().set(quota_key(me["id"]), 10**9)
    r = await c.post("/api/sessions", json={"snapshot_id": analyzed["snapshot_id"]})
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"
    await c.aclose()


async def test_incremental_analysis(user_client: httpx.AsyncClient, analyzed: dict[str, Any]) -> None:
    repo = analyzed["repo_path"]
    commit_change(
        repo,
        "src/main/java/com/example/feed/listener/LikeListener.java",
        'log.info("notify author of feed {}", feedId);',
        'log.info("notify author {}", feedId);',
    )
    r = await user_client.post(f"/api/repos/{analyzed['repository_id']}/refresh")
    assert r.status_code == 200 and r.json()["new_commit"] is True
    await drain()
    snap = (await user_client.get(f"/api/snapshots/{r.json()['snapshot_id']}")).json()
    assert snap["status"] == "ready", snap
    st = snap["stats"]
    assert st["files_changed"] == 1 and st["topics_reused"] > 0 and st["questions_reused"] > 0
    assert st["parse_cache_hits"] >= st["java_files"] - 1
    assert st["chunks_reused"] > 0
    assert snap["base_snapshot_id"] == analyzed["snapshot_id"]
