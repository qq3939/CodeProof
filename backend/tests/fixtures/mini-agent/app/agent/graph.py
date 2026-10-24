from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class State(TypedDict):
    question: str
    draft: str
    approved: bool


def draft_answer(state: State) -> dict:
    return {"draft": "关于 " + state["question"] + " 的回答"}


def human_review(state: State) -> dict:
    # 暂停等人工确认，恢复时用 Command(resume=...) 传回结果
    decision = interrupt({"draft": state["draft"]})
    return {"approved": bool(decision)}


def route_after_review(state: State) -> str:
    return "publish" if state["approved"] else "draft_answer"


def publish(state: State) -> dict:
    return {}


def build_graph():
    g = StateGraph(State)
    g.add_node("draft_answer", draft_answer)
    g.add_node("human_review", human_review)
    g.add_node("publish", publish)
    g.add_edge(START, "draft_answer")
    g.add_edge("draft_answer", "human_review")
    g.add_conditional_edges("human_review", route_after_review, {"publish": "publish", "draft_answer": "draft_answer"})
    g.add_edge("publish", END)
    return g.compile(checkpointer=MemorySaver())


def answer(question: str, thread_id: str) -> dict:
    graph = build_graph()
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 10}
    return graph.invoke({"question": question, "draft": "", "approved": False}, config)
