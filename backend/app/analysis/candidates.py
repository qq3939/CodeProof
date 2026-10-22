"""候选考点：从画像和代码模型里确定性地找出"值得问的点"。

考点生成（SPEC 7.5）分两步：
1. 这里先用规则找候选（链路、机制用法、可疑写法），每个都带真实的证据位置；
2. 再交给快模型挑选、改写说明、补充设计决策类考点。
先规则后模型，是为了让模型"看着证据说话"，而不是凭空编一个仓库里不存在的考点。
"""

from __future__ import annotations

import re
from typing import Any

from app.analysis.code_model import CodeModel, SymRef
from app.analysis.spring_profile import is_test_path

MAX_EVIDENCE_LINES = 40


def _ev(path: str, start: int, end: int) -> dict[str, Any]:
    end = min(end, start + MAX_EVIDENCE_LINES - 1)
    return {"path": path, "start_line": start, "end_line": max(start, end)}


def _ev_sym(s: SymRef) -> dict[str, Any]:
    return _ev(s.path, s.sym.start_line, s.sym.end_line)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _self_invocations(model: CodeModel, annotation: str) -> list[tuple[SymRef, SymRef, int]]:
    """找"同类内部直接调用带 @Transactional/@Async/@Cacheable 的方法"——Spring AOP 代理不生效的经典坑。"""
    out = []
    for rc in model.calls:
        if rc.call.receiver not in (None, "this") or is_test_path(rc.caller.path):
            continue
        for t in rc.targets:
            if t.has_ann(annotation) and t.owner is rc.caller.owner and t.id != rc.caller.id:
                out.append((rc.caller, t, rc.call.line))
    return out


def build_candidates(model: CodeModel, profile: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []
    usages = profile.get("usages", {})

    # ---- 链路：每个接口一条，优先"层次多、碰到中间件"的接口
    for ch in profile.get("chains", []):
        steps = ch["steps"]
        if len(steps) < 2:
            continue
        layers = {st["layer"] for st in steps}
        richness = len(layers) + (2 if "middleware" in layers else 0) + min(len(steps), 8) / 4
        evs = [_ev(st["file"], st["start_line"], st["end_line"]) for st in steps[:4] if st.get("file")]
        idents = [st["symbol"].split(".")[-1] for st in steps[:3]]
        c.append(
            {
                "kind": "chain",
                "title": f"接口 {ch['endpoint']} 的调用链路",
                "description": "从 " + " → ".join(st["symbol"] for st in steps[:5]) + " 的处理过程",
                "evidence": evs,
                "importance": 5 if richness >= 6 else (4 if richness >= 4 else 3),
                "topic_key": f"chain:{ch['endpoint']}",
                "identifiers": idents,
                "_score": richness,
            }
        )

    # ---- 机制用法
    labels = {
        "transaction": ("事务", "tx"),
        "cache": ("缓存", "cache"),
        "redis": ("Redis 用法", "redis"),
        "mq": ("消息队列", "mq"),
        "scheduled": ("定时任务", "scheduled"),
        "async": ("异步执行", "async"),
        "concurrency": ("并发控制", "concurrency"),
    }
    for key, (label, prefix) in labels.items():
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for u in usages.get(key, []):
            by_symbol.setdefault(u["symbol"], []).append(u)
        for rank, (sym, items) in enumerate(list(by_symbol.items())[:4]):
            first = items[0]
            detail = ""
            if key == "transaction":
                detail = f"，传播行为 {first.get('propagation')}，回滚规则 {first.get('rollback_for') or '默认（仅运行时异常）'}"
            elif key in ("redis", "mq") and first.get("call"):
                detail = "，调用 " + "、".join(sorted({i.get("call", "") for i in items}))[:120]
            elif key == "concurrency":
                detail = "，方式：" + "、".join(sorted({i.get("kind", "") for i in items}))
            ident = sym.split(".")[-1]
            c.append(
                {
                    "kind": "mechanism",
                    "title": f"{sym} 中的{label}",
                    "description": f"{sym} 使用了{label}{detail}",
                    "evidence": [_ev(first["file"], first["start_line"], first["end_line"])],
                    "importance": 4 if key in ("transaction", "redis", "mq", "concurrency") else 3,
                    "topic_key": f"{prefix}:{sym}",
                    # 轮转：每类机制的第一个先入选，避免某一类（如 Redis 有上百处）占满名额
                    "_score": 10 - rank,
                    "identifiers": [ident],
                }
            )

    # ---- 风险点：AOP 自调用
    for ann, label in (("Transactional", "事务"), ("Async", "异步"), ("Cacheable", "缓存")):
        for caller, target, line in _self_invocations(model, ann):
            c.append(
                {
                    "kind": "risk",
                    "title": f"{caller.short} 内部直接调用 @{ann} 方法 {target.sym.name}",
                    "description": f"同一个类里通过 this 调用带 @{ann} 的方法，不经过 Spring 代理，{label}注解可能不生效",
                    "evidence": [_ev(caller.path, max(caller.sym.start_line, line - 3), line + 3), _ev_sym(target)],
                    "importance": 5,
                    "topic_key": f"risk.self-invoke-{ann.lower()}:{target.short}",
                    "identifiers": [target.sym.name],
                }
            )
    # ---- 风险点：@Transactional 未指定 rollbackFor / 非 public
    for u in usages.get("transaction", []):
        if not u.get("rollback_for"):
            c.append(
                {
                    "kind": "risk",
                    "title": f"{u['symbol']} 的事务回滚规则",
                    "description": "@Transactional 没有指定 rollbackFor，受检异常默认不会回滚",
                    "evidence": [_ev(u["file"], u["start_line"], u["end_line"])],
                    "importance": 3,
                    "topic_key": f"risk.tx-rollback:{u['symbol']}",
                    "identifiers": ["Transactional"],
                }
            )
    for s in model.symbols:
        if (
            s.sym.kind == "method"
            and s.has_ann("Transactional")
            and not re.search(r"\bpublic\b", s.sym.signature)
            and not is_test_path(s.path)
        ):
            c.append(
                {
                    "kind": "risk",
                    "title": f"非 public 方法 {s.short} 上的 @Transactional",
                    "description": "基于代理的事务对非 public 方法不生效",
                    "evidence": [_ev_sym(s)],
                    "importance": 4,
                    "topic_key": f"risk.tx-nonpublic:{s.short}",
                    "identifiers": [s.sym.name],
                }
            )

    # ---- 设计决策：同一个业务方法里组合使用了多种中间件
    combo: dict[str, set[str]] = {}
    loc: dict[str, dict[str, Any]] = {}
    for key in ("redis", "mq", "cache", "transaction", "concurrency"):
        for u in usages.get(key, []):
            combo.setdefault(u["symbol"], set()).add(key)
            loc[u["symbol"]] = u
    for sym, kinds in combo.items():
        if len(kinds) >= 2:
            u = loc[sym]
            c.append(
                {
                    "kind": "design",
                    "title": f"{sym} 的整体设计",
                    "description": f"{sym} 同时用到了 {'、'.join(sorted(kinds))}，为什么这样组合？有哪些取舍？",
                    "evidence": [_ev(u["file"], u["start_line"], u["end_line"])],
                    "importance": 5,
                    "topic_key": f"design:{sym}",
                    "identifiers": [sym.split(".")[-1]],
                }
            )
    # 定时任务 + Redis：经典的"先写缓存、再批量落库"
    if usages.get("scheduled") and usages.get("redis"):
        sch = usages["scheduled"][0]
        c.append(
            {
                "kind": "design",
                "title": "Redis 计数与定时落库的设计",
                "description": "数据先写 Redis，再由定时任务批量写回数据库。一致性、丢数据风险和刷库频率怎么权衡？",
                "evidence": [
                    _ev(sch["file"], sch["start_line"], sch["end_line"]),
                    _ev(usages["redis"][0]["file"], usages["redis"][0]["start_line"], usages["redis"][0]["end_line"]),
                ],
                "importance": 5,
                "topic_key": "design.redis-batch-flush",
                "identifiers": [sch["symbol"].split(".")[-1]],
            }
        )

    # 去重 + 按重要度排序；每类设上限，保证覆盖面（大仓库里接口有上百个，不能让链路题挤满题库）
    caps = {"chain": max(3, limit // 5), "mechanism": limit // 3, "risk": limit // 5, "design": limit // 6 + 1}
    seen: set[str] = set()
    per_kind: dict[str, int] = {}
    uniq = []
    for item in sorted(c, key=lambda x: (-x["importance"], -x.get("_score", 0), x["topic_key"])):
        if item["topic_key"] in seen or per_kind.get(item["kind"], 0) >= caps.get(item["kind"], limit):
            continue
        seen.add(item["topic_key"])
        per_kind[item["kind"]] = per_kind.get(item["kind"], 0) + 1
        item.pop("_score", None)
        item["files"] = sorted({e["path"] for e in item["evidence"]})
        uniq.append(item)
    return uniq[:limit]


def topic_key_slug(key: str) -> str:
    return _slug(key)
