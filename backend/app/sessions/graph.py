"""面试官图（LangGraph StateGraph，SPEC 8.3）。

    START → select_question → ask → await_answer ──answer──> assess → verify_with_code → decide
                 ↑                     │  ↑                                              │
                 │                     │  └──────────── follow_up <──── 有缺口且没到深度上限 ┤
                 │                     ├──hint──> give_hint ──┘                           │
                 └──── close_question <┴──skip                        否则 ───────────────┤
                                                                       题量用完/预算不足 → finish → END

- await_answer 用 interrupt() 暂停，等用户作答后用 Command(resume=...) 恢复；
- 检查点存在 Postgres（AsyncPostgresSaver），thread_id 就是 session_id，
  所以刷新页面、甚至 api 进程重启后，都能从断点接着答；
- 流式事件通过 get_stream_writer() 发出，api 用 stream_mode="custom" 读取并转成 SSE。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import func, select, update

from app.agents import interviewer
from app.agents.schemas import QuickAssessOut
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.queue import enqueue
from app.core.text import code_like, identifiers
from app.llm.types import CallContext
from app.models import ExamSession, SessionTurn
from app.sessions.planner import materialize
from app.tools.sandbox import Sandbox

logger = logging.getLogger(__name__)
MAX_VERIFY_CALLS = 5


class InterviewState(TypedDict, total=False):
    session_id: str
    snapshot_id: str
    user_id: str
    mode: str
    max_depth: int
    allow_hints: bool
    style: str
    plan: list[dict[str, Any]]
    index: int  # 当前是计划里的第几题（从 0 开始）
    asked: int  # 已经问过的题数
    current: dict[str, Any] | None
    depth: int  # 当前题已追问的层数
    hint_level: int
    answers: list[str]  # 当前题的全部回答
    last_answer: str
    action: str  # answer / hint / skip / finish
    assess: dict[str, Any] | None
    verify_notes: list[str]
    coverage: list[str]  # 已考过的 topic_key
    finish_reason: str | None
    route: str


# ---------------------------------------------------------------- 工具函数
def _emit(event: str, data: dict[str, Any]) -> None:
    try:
        get_stream_writer()({"event": event, "data": data})
    except RuntimeError:  # 不在流式上下文里（例如单测直接调用节点）
        pass


async def _stream_text(chunks: Any) -> str:
    out = []
    async for piece in chunks:
        out.append(piece)
        _emit("token", {"text": piece})
    return "".join(out)


async def _emit_text(text: str) -> None:
    for i in range(0, len(text), 12):
        _emit("token", {"text": text[i : i + 12]})


async def add_turn(
    session_id: str, role: str, content: str, question_id: str | None, depth: int, meta: dict[str, Any] | None = None
) -> None:
    async with session_scope() as db:
        seq = (
            int(
                (
                    await db.execute(
                        select(func.coalesce(func.max(SessionTurn.seq), 0)).where(
                            SessionTurn.session_id == uuid.UUID(session_id)
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        db.add(
            SessionTurn(
                session_id=uuid.UUID(session_id),
                seq=seq,
                role=role,
                content=content,
                question_id=uuid.UUID(question_id) if question_id else None,
                depth=depth,
                meta=meta,
            )
        )
        await db.commit()


def _ctx(state: InterviewState, node: str) -> CallContext:
    return CallContext(node=node, user_id=uuid.UUID(state["user_id"]), session_id=uuid.UUID(state["session_id"]))


async def _sandbox(state: InterviewState, ctx: CallContext, on_tool: Any = None) -> Sandbox:
    async with session_scope() as db:
        sess = await db.get(ExamSession, uuid.UUID(state["session_id"]))
        assert sess is not None
        change_dir = (sess.config or {}).get("change_dir")
    if change_dir:
        root = change_dir
    else:
        from app.repos.storage import ensure_checkout

        root = await ensure_checkout(uuid.UUID(state["snapshot_id"]))
    return Sandbox(
        root,
        uuid.UUID(state["snapshot_id"]),
        ctx=ctx,
        kb_user_id=uuid.UUID(state["user_id"]),
        allow_kb=state.get("mode") == "kb_link",
        allow_vector=get_settings().retrieval_mode in ("vector", "hybrid"),
        on_tool=on_tool,
    )


async def _budget_left(state: InterviewState) -> tuple[bool, str | None]:
    async with session_scope() as db:
        sess = await db.get(ExamSession, uuid.UUID(state["session_id"]))
        assert sess is not None
        budget = int((sess.config or {}).get("token_budget") or get_settings().session_token_budget)
        if sess.total_tokens >= budget * 0.9:
            return False, f"本场会话的 Token 预算（{budget}）快用完了，先收尾，已答的题照常阅卷"
    return True, None


# ---------------------------------------------------------------- 节点
async def select_question(state: InterviewState) -> dict[str, Any]:
    plan = state.get("plan", [])
    idx = state.get("index", 0)
    ok, reason = await _budget_left(state)
    if not ok:
        return {"route": "finish", "finish_reason": reason, "current": None}
    ctx = _ctx(state, "select_question")
    while idx < len(plan):
        item = plan[idx]
        sandbox = await _sandbox(state, ctx)
        q = await materialize(item, await _load_session(state), sandbox, ctx)
        if q is not None:
            return {
                "route": "ask",
                "index": idx,
                "current": q,
                "depth": 0,
                "hint_level": 0,
                "answers": [],
                "assess": None,
                "verify_notes": [],
            }
        idx += 1  # 这一项生成失败（证据校验不过），跳到下一项
    return {
        "route": "finish",
        "finish_reason": state.get("finish_reason") or "题目已经全部问完",
        "current": None,
        "index": idx,
    }


async def _load_session(state: InterviewState) -> ExamSession:
    async with session_scope() as db:
        sess = await db.get(ExamSession, uuid.UUID(state["session_id"]))
        assert sess is not None
        return sess


async def ask(state: InterviewState) -> dict[str, Any]:
    q = state["current"]
    assert q is not None
    total = len(state.get("plan", []))
    n = state.get("asked", 0) + 1
    _emit(
        "next_question",
        {"question_id": q["question_id"], "index": n, "total": total, "topic": q.get("title"), "kind": q.get("kind")},
    )
    prefix = f"第 {n} 题。" if state.get("style") == "interviewer" else ""
    text = await _stream_text(interviewer.ask_stream(q, state.get("style", "plain"), _ctx(state, "ask"), prefix))
    await add_turn(
        state["session_id"],
        "examiner",
        text,
        q["question_id"],
        0,
        {"kind": "question", "index": n, "total": total, "topic": q.get("title")},
    )
    return {"asked": n}


async def await_answer(state: InterviewState) -> dict[str, Any]:
    q = state.get("current") or {}
    value = interrupt({"awaiting": "answer", "question_id": q.get("question_id"), "depth": state.get("depth", 0)})
    if not isinstance(value, dict):
        value = {"action": "answer", "text": str(value)}
    return {"action": value.get("action", "answer"), "last_answer": str(value.get("text", ""))}


def route_after_answer(state: InterviewState) -> str:
    a = state.get("action", "answer")
    return {"hint": "give_hint", "skip": "close_question", "finish": "finish"}.get(a, "assess")


async def assess(state: InterviewState) -> dict[str, Any]:
    q = state["current"]
    assert q is not None
    text = state.get("last_answer", "")
    await add_turn(state["session_id"], "user", text, q["question_id"], state.get("depth", 0))
    answers = [*state.get("answers", []), text]
    out = await interviewer.quick_assess(q, "\n".join(answers), _ctx(state, "quick_assess"))
    return {"assess": out.model_dump(), "answers": answers}


async def verify_with_code(state: InterviewState) -> dict[str, Any]:
    """回答里出现关于代码的具体说法时，调工具去核对；每轮最多 5 次工具调用（SPEC 8.3）。"""
    q = state["current"]
    a = QuickAssessOut.model_validate(state.get("assess") or {"category": "partial"})
    claims = [c.lstrip("@") for c in a.code_claims]
    if not claims:
        claims = [i.lstrip("@") for i in identifiers(state.get("last_answer", "")) if code_like(i)][:3]
    if not claims or q is None:
        return {"verify_notes": []}
    ctx = _ctx(state, "verify_with_code")

    async def on_tool(name: str, args: dict[str, Any]) -> None:
        target = args.get("path") or args.get("name") or args.get("pattern") or args.get("symbol") or ""
        _emit("tool", {"tool": name, "target": target, "message": f"面试官正在查看 {target}"})

    sandbox = await _sandbox(state, ctx, on_tool=on_tool)
    notes: list[str] = []
    calls = 0
    for claim in claims[:3]:
        if calls >= MAX_VERIFY_CALLS:
            break
        name = re.split(r"[.(]", claim)[-1] if "." in claim else claim
        out = await sandbox.call("find_symbol", {"name": name}, node="verify_with_code")
        calls += 1
        if out.startswith("(没有找到"):
            if calls >= MAX_VERIFY_CALLS:
                break
            g = await sandbox.call("grep", {"pattern": re.escape(name)}, node="verify_with_code")
            calls += 1
            if g.startswith("(没有匹配"):
                notes.append(f"你提到的 `{name}`，我在代码里没有找到，能说说它在哪个文件吗？")
                continue
            first = g.splitlines()[0].split(":")[0]
        else:
            m = re.search(r" @ (\S+?):(\d+)-(\d+)", out)
            first = m.group(1) if m else ""
            if m and calls < MAX_VERIFY_CALLS:
                await sandbox.call(
                    "read_file",
                    {"path": m.group(1), "start": int(m.group(2)), "end": min(int(m.group(3)), int(m.group(2)) + 60)},
                    node="verify_with_code",
                )
                calls += 1
        logger.debug("verified %s in %s", name, first)
    return {"verify_notes": notes}


async def decide(state: InterviewState) -> dict[str, Any]:
    ok, reason = await _budget_left(state)
    if not ok:
        return {"route": "finish", "finish_reason": reason}
    a = QuickAssessOut.model_validate(state.get("assess") or {"category": "partial"})
    gaps = a.category != "complete" and (a.missing_points or state.get("verify_notes"))
    if a.category == "unknown":
        # 用户明确说不会：不再追问，留给他用提示或者直接下一题
        return {"route": "close_question"}
    if gaps and state.get("depth", 0) < state.get("max_depth", 2):
        return {"route": "follow_up"}
    return {"route": "close_question"}


async def follow_up(state: InterviewState) -> dict[str, Any]:
    q = state["current"]
    assert q is not None
    a = QuickAssessOut.model_validate(state.get("assess") or {"category": "partial"})
    depth = state.get("depth", 0) + 1
    text = await interviewer.follow_up(
        q, a, state.get("verify_notes", []), depth - 1, _ctx(state, "follow_up"), state.get("answers", [])
    )
    _emit("followup", {"question_id": q["question_id"], "depth": depth})
    await _emit_text(text)
    await add_turn(state["session_id"], "examiner", text, q["question_id"], depth, {"kind": "followup"})
    return {"depth": depth}


async def give_hint(state: InterviewState) -> dict[str, Any]:
    q = state["current"]
    assert q is not None
    if not state.get("allow_hints", True):
        _emit("error", {"message": "本场考核不允许使用提示"})
        return {}
    level = min(3, state.get("hint_level", 0) + 1)
    if state.get("hint_level", 0) >= 3:
        _emit("error", {"message": "三级提示已经用完了"})
        return {}
    text = await interviewer.give_hint(
        q,
        level,
        _ctx(state, "give_hint"),
        state.get("answers", []),
        q.get("next_station") if state.get("mode") == "chain" else None,
    )
    penalty = interviewer.hint_penalty(level)
    _emit("hint", {"question_id": q["question_id"], "level": level, "text": text, "penalty": penalty})
    await add_turn(
        state["session_id"], "hint", text, q["question_id"], state.get("depth", 0), {"level": level, "penalty": penalty}
    )
    return {"hint_level": level}


async def close_question(state: InterviewState) -> dict[str, Any]:
    """一道题结束：投递异步阅卷任务（SPEC 8.4 时机），推进到下一题。"""
    q = state.get("current")
    if q is not None:
        if state.get("action") == "skip":
            await add_turn(
                state["session_id"],
                "system",
                "用户跳过了这道题",
                q["question_id"],
                state.get("depth", 0),
                {"skipped": True},
            )
        await enqueue(
            "grade", {"session_id": state["session_id"], "question_id": q["question_id"]}, uuid.UUID(state["user_id"])
        )
    coverage = [*state.get("coverage", []), *([q["topic_key"]] if q else [])]
    asked = state.get("asked", 0)
    sess = await _load_session(state)
    nxt = state.get("index", 0) + 1
    if asked >= sess.question_count or nxt >= len(state.get("plan", [])):
        return {
            "route": "finish",
            "coverage": coverage,
            "current": None,
            "index": nxt,
            "finish_reason": "题目已经全部问完",
        }
    return {"route": "select_question", "coverage": coverage, "current": None, "index": nxt}


async def finish(state: InterviewState) -> dict[str, Any]:
    q = state.get("current")
    reason = state.get("finish_reason") or ("用户提前结束" if state.get("action") == "finish" else "题目已经全部问完")
    if q is not None:
        # 提前结束或预算不足：当前这道题如果答过，照常阅卷
        async with session_scope() as db:
            n = (
                await db.execute(
                    select(func.count())
                    .select_from(SessionTurn)
                    .where(
                        SessionTurn.session_id == uuid.UUID(state["session_id"]),
                        SessionTurn.role == "user",
                        SessionTurn.question_id == uuid.UUID(q["question_id"]),
                    )
                )
            ).scalar_one()
        if n:
            await enqueue(
                "grade",
                {"session_id": state["session_id"], "question_id": q["question_id"]},
                uuid.UUID(state["user_id"]),
            )
    async with session_scope() as db:
        await db.execute(
            update(ExamSession)
            .where(ExamSession.id == uuid.UUID(state["session_id"]))
            .values(status="grading", finish_reason=reason, finished_at=datetime.now(UTC))
        )
        await db.commit()
    await add_turn(state["session_id"], "system", f"考核结束：{reason}", None, 0, {"kind": "finished"})
    await enqueue("report", {"session_id": state["session_id"]}, uuid.UUID(state["user_id"]))
    _emit("session_finished", {"reason": reason})
    return {"current": None, "finish_reason": reason}


def _route(state: InterviewState) -> str:
    return state.get("route", "finish")


def build_graph() -> StateGraph:
    g = StateGraph(InterviewState)
    for name, fn in (
        ("select_question", select_question),
        ("ask", ask),
        ("await_answer", await_answer),
        ("assess", assess),
        ("verify_with_code", verify_with_code),
        ("decide", decide),
        ("follow_up", follow_up),
        ("give_hint", give_hint),
        ("close_question", close_question),
        ("finish", finish),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "select_question")
    g.add_conditional_edges("select_question", _route, {"ask": "ask", "finish": "finish"})
    g.add_edge("ask", "await_answer")
    g.add_conditional_edges(
        "await_answer",
        route_after_answer,
        {"assess": "assess", "give_hint": "give_hint", "close_question": "close_question", "finish": "finish"},
    )
    g.add_edge("assess", "verify_with_code")
    g.add_edge("verify_with_code", "decide")
    g.add_conditional_edges(
        "decide", _route, {"follow_up": "follow_up", "close_question": "close_question", "finish": "finish"}
    )
    g.add_edge("follow_up", "await_answer")
    g.add_edge("give_hint", "await_answer")
    g.add_conditional_edges("close_question", _route, {"select_question": "select_question", "finish": "finish"})
    g.add_edge("finish", END)
    return g


# ---------------------------------------------------------------- 检查点
_pool: AsyncConnectionPool | None = None
_graph: CompiledStateGraph | None = None


async def get_graph() -> CompiledStateGraph:
    global _pool, _graph
    if _graph is None:
        _pool = AsyncConnectionPool(
            get_settings().sync_database_url,
            max_size=10,
            open=False,
            # AsyncPostgresSaver 要求 autocommit + dict_row；prepare_threshold=0 兼容 pgbouncer
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await _pool.open()
        saver = AsyncPostgresSaver(_pool)  # type: ignore[arg-type]
        await saver.setup()  # 幂等：建检查点表
        _graph = build_graph().compile(checkpointer=saver)
    return _graph


async def close_checkpointer() -> None:
    """关闭连接池。测试里用它模拟"api 重启"：之后 get_graph() 会重新建连接、从 Postgres 读检查点。"""
    global _pool, _graph
    if _pool is not None:
        await _pool.close()
    _pool, _graph = None, None
