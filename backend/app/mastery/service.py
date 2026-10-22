"""掌握度：EMA 分数 + 间隔复习排期 + 选题优先级（SPEC 9.2）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Mastery

ALPHA = 0.5


def ema(prev: float | None, score: float, alpha: float = ALPHA) -> float:
    """指数移动平均：新分数权重 α，旧分数权重 1-α。α=0.5 时最近一次影响最大，但不会一次就清零历史。"""
    return score if prev is None else alpha * score + (1 - alpha) * prev


def next_interval(score: float, high_streak: int) -> timedelta:
    """按分数段安排下次复习：<60 → 1 天；60–80 → 3 天；>80 → 7 天；连续两次 >80 → 21 天。"""
    if score > 80:
        return timedelta(days=21) if high_streak >= 2 else timedelta(days=7)
    if score >= 60:
        return timedelta(days=3)
    return timedelta(days=1)


def apply_score(
    m: Mastery | None, score: float, now: datetime, session_id: str, title: str | None = None
) -> dict[str, Any]:
    """返回更新后的字段（纯函数，便于单测）。"""
    prev = m.score_ema if m and m.attempts else None
    streak = (m.high_streak if m else 0) + 1 if score > 80 else 0
    new_ema = ema(prev, score)
    history = list(m.history) if m else []
    history.append({"at": now.isoformat(), "score": score, "ema": round(new_ema, 1), "session_id": session_id})
    return {
        "score_ema": new_ema,
        "attempts": (m.attempts if m else 0) + 1,
        "high_streak": streak,
        "history": history[-50:],
        "last_seen_at": now,
        "next_review_at": now + next_interval(score, streak),
        "title": title or (m.title if m else None),
    }


async def record(
    db: AsyncSession,
    user_id: uuid.UUID,
    repository_id: uuid.UUID,
    topic_key: str,
    score: float,
    session_id: str,
    title: str | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    m = await db.get(Mastery, (user_id, repository_id, topic_key))
    vals = apply_score(m, score, now, session_id, title)
    if m is None:
        db.add(Mastery(user_id=user_id, repository_id=repository_id, topic_key=topic_key, **vals))
    else:
        for k, v in vals.items():
            setattr(m, k, v)


async def load(db: AsyncSession, user_id: uuid.UUID, repository_id: uuid.UUID) -> dict[str, Mastery]:
    rows = (
        await db.execute(select(Mastery).where(Mastery.user_id == user_id, Mastery.repository_id == repository_id))
    ).scalars()
    return {m.topic_key: m for m in rows}


def priority(topic_key: str, importance: int, kind: str, mastery: dict[str, Mastery], now: datetime) -> float:
    """选题优先级：到期复习 > 薄弱点 > 没考过 > 已掌握；重要度作为次要因素。"""
    m = mastery.get(topic_key)
    p = importance / 5
    if m is None or m.attempts == 0:
        return p + 1.0
    if m.next_review_at and m.next_review_at <= now:
        p += 3.0
    if m.score_ema < 60:
        p += 2.0 + (60 - m.score_ema) / 30
    elif m.score_ema > 80:
        p -= 1.0
    return p


def pick(
    items: list[dict[str, Any]], n: int, mastery: dict[str, Mastery], difficulty: str, now: datetime | None = None
) -> list[dict[str, Any]]:
    """从候选题里挑 n 道：按优先级排序，同时兼顾覆盖面（同一考点只取一题、同类型不扎堆）和难度。"""
    now = now or datetime.now(UTC)
    scored = []
    for it in items:
        p = priority(it["topic_key"], it.get("importance", 3), it.get("kind", ""), mastery, now)
        if it.get("difficulty") == difficulty:
            p += 0.3
        scored.append((p, it))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("question_id") or x[1]["topic_key"])))
    chosen: list[dict[str, Any]] = []
    keys: set[str] = set()
    kind_count: dict[str, int] = {}
    for _, it in scored:
        if it["topic_key"] in keys:
            continue
        k = it.get("kind", "")
        # 同一类型最多占一半，保证覆盖面
        if kind_count.get(k, 0) >= max(1, (n + 1) // 2) and len(scored) > n:
            continue
        chosen.append(it)
        keys.add(it["topic_key"])
        kind_count[k] = kind_count.get(k, 0) + 1
        if len(chosen) >= n:
            break
    if len(chosen) < n:  # 覆盖面约束导致不够，再按优先级补齐
        for _, it in scored:
            if it not in chosen and it["topic_key"] not in keys:
                chosen.append(it)
                keys.add(it["topic_key"])
                if len(chosen) >= n:
                    break
    return chosen
