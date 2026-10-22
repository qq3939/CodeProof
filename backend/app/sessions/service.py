"""考核会话的业务逻辑：创建、推进（作答/提示/跳过/结束）、查询状态。

推进图的方式（见 D-008）：图在后台任务里跑，事件写进 asyncio.Queue，SSE 只负责从队列读。
这样即使用户中途关掉页面（SSE 断开），图也会跑完当前这一步并存好检查点，
不会出现"节点执行到一半被取消、下次重放导致重复记录"的问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import AppError, Conflict, NotFound, TooManyRequests
from app.core.logging import session_id_var
from app.core.queue import enqueue
from app.core.redis import get_redis
from app.llm import accounting
from app.models import ExamSession, RepoSnapshot, SessionTurn, User
from app.sessions.graph import get_graph
from app.sessions.planner import MODES, build_plan

logger = logging.getLogger(__name__)


def _config(sid: uuid.UUID) -> dict[str, Any]:
    return {"configurable": {"thread_id": str(sid)}}


async def create_session(db: AsyncSession, user: User, body: dict[str, Any]) -> ExamSession:
    s = get_settings()
    mode = body["mode"]
    if mode not in MODES:
        raise AppError(f"不支持的模式 {mode}")
    snap = await db.get(RepoSnapshot, body["snapshot_id"])
    if snap is None or snap.status != "ready":
        raise AppError("快照还没有分析完成", code="snapshot_not_ready")
    active = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ExamSession)
                .where(ExamSession.user_id == user.id, ExamSession.status.in_(["active", "preparing"]))
            )
        ).scalar_one()
    )
    if active >= s.max_active_sessions_per_user:
        raise TooManyRequests(f"同时最多进行 {s.max_active_sessions_per_user} 场会话，请先结束其他会话")
    if await accounting.used_today(user.id) >= s.daily_token_quota:
        raise AppError("今日 Token 配额已用完", code="quota_exceeded", status_code=429)
    cfg = dict(body.get("config") or {})
    cfg.setdefault("allow_hints", body.get("allow_hints", True))
    cfg.setdefault("style", body.get("style", "plain"))
    sess = ExamSession(
        user_id=user.id,
        snapshot_id=snap.id,
        mode=mode,
        difficulty=body.get("difficulty", "medium"),
        question_count=int(body.get("question_count", 5)),
        max_depth=min(int(body.get("max_depth", 2)), s.max_followup_depth),
        status="preparing" if mode == "change" else "active",
        config=cfg,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    if mode == "change":
        await enqueue("change_prepare", {"session_id": sess.id}, user.id)
    return sess


async def start_graph(session_id: uuid.UUID) -> list[dict[str, Any]]:
    """建好出题计划，跑到第一题的 interrupt 为止。返回过程中产生的事件。"""
    async with session_scope() as db:
        sess = await db.get(ExamSession, session_id)
        assert sess is not None
    try:
        plan = await build_plan(sess)
    except AppError:
        async with session_scope() as db:
            row = await db.get(ExamSession, session_id)
            assert row is not None
            row.status = "failed"
            await db.commit()
        raise
    async with session_scope() as db:
        row = await db.get(ExamSession, session_id)
        assert row is not None
        row.config = {**(row.config or {}), "plan": plan}
        if row.question_count > len(plan):
            row.question_count = len(plan)
        await db.commit()
    init = {
        "session_id": str(sess.id),
        "snapshot_id": str(sess.snapshot_id),
        "user_id": str(sess.user_id),
        "mode": sess.mode,
        "max_depth": sess.max_depth,
        "allow_hints": bool(sess.config.get("allow_hints", True)),
        "style": sess.config.get("style", "plain"),
        "plan": plan,
        "index": 0,
        "asked": 0,
        "coverage": [],
    }
    graph = await get_graph()
    events = []
    async for ev in graph.astream(init, _config(sess.id), stream_mode="custom"):
        events.append(ev)
    return events


async def _lock(session_id: uuid.UUID) -> bool:
    return bool(await get_redis().set(f"sess-lock:{session_id}", "1", nx=True, ex=180))


async def _unlock(session_id: uuid.UUID) -> None:
    await get_redis().delete(f"sess-lock:{session_id}")


async def load_owned(db: AsyncSession, session_id: uuid.UUID, user: User) -> ExamSession:
    sess = await db.get(ExamSession, session_id)
    if sess is None or sess.user_id != user.id:  # 数据隔离：会话是个人私有的
        raise NotFound("会话不存在")
    return sess


async def resume_stream(session_id: uuid.UUID, value: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """用 Command(resume=value) 推进图，逐个产出事件。"""
    if not await _lock(session_id):
        raise Conflict("上一个请求还在处理中，请稍候")
    session_id_var.set(str(session_id))
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def runner() -> None:
        try:
            graph = await get_graph()
            snap = await graph.aget_state(_config(session_id))
            if not snap.next:
                await queue.put({"event": "error", "data": {"message": "会话已经结束"}})
                return
            async for ev in graph.astream(Command(resume=value), _config(session_id), stream_mode="custom"):
                await queue.put(ev)
        except Exception as e:  # noqa: BLE001
            logger.exception("graph run failed")
            await queue.put(
                {"event": "error", "data": {"message": getattr(e, "message", None) or "面试官出错了，请重试"}}
            )
        finally:
            await _unlock(session_id)
            await queue.put(None)

    task = asyncio.create_task(runner())
    _running.add(task)
    task.add_done_callback(_running.discard)
    while True:
        ev = await queue.get()
        if ev is None:
            break
        yield ev
    yield {"event": "done", "data": await state_view(session_id)}


_running: set[asyncio.Task[None]] = set()


async def state_view(session_id: uuid.UUID) -> dict[str, Any]:
    """前端需要的会话状态：当前第几题、追问深度、提示级别、是否在等作答。"""
    graph = await get_graph()
    snap = await graph.aget_state(_config(session_id))
    v = snap.values or {}
    cur = v.get("current") or {}
    async with session_scope() as db:
        sess = await db.get(ExamSession, session_id)
    return {
        "status": sess.status if sess else None,
        "awaiting_answer": bool(snap.next) and "await_answer" in snap.next,
        "index": v.get("asked", 0),
        "total": sess.question_count if sess else 0,
        "depth": v.get("depth", 0),
        "max_depth": v.get("max_depth"),
        "hint_level": v.get("hint_level", 0),
        "allow_hints": v.get("allow_hints", True),
        "current_topic": cur.get("title"),
        "current_kind": cur.get("kind"),
        "question_id": cur.get("question_id"),
        "coverage": v.get("coverage", []),
        "finish_reason": v.get("finish_reason"),
    }


async def turns(db: AsyncSession, session_id: uuid.UUID) -> list[SessionTurn]:
    return list(
        (await db.execute(select(SessionTurn).where(SessionTurn.session_id == session_id).order_by(SessionTurn.seq)))
        .scalars()
        .all()
    )


def sse_format(ev: dict[str, Any]) -> str:
    return f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False, default=str)}\n\n"
