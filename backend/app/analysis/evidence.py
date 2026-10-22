"""证据校验（SPEC 7.5 / 7.6）。

模型最常见的"幻觉"是编造行号或文件。规则：
1. 文件存在、行范围真实存在；
2. 片段里要出现考点提到的标识符（至少一个）。
校验不通过 → 让模型重新生成一次 → 还不通过就丢弃。
"""

from __future__ import annotations

from typing import Any

from app.core.text import code_like, identifiers
from app.tools.sandbox import Sandbox


def snippet(sandbox: Sandbox, ev: dict[str, Any], pad: int = 0) -> str | None:
    lines = sandbox.read_lines_raw(ev["path"], int(ev["start_line"]), int(ev["end_line"]))
    if lines is None:
        return None
    if pad:
        full = sandbox.read_lines_raw(ev["path"], max(1, int(ev["start_line"]) - pad), int(ev["end_line"]))
        if full is not None:
            lines = full
    return "\n".join(lines)


def check(sandbox: Sandbox, evidence: list[dict[str, Any]], idents: list[str], fallback_text: str = "") -> str | None:
    """通过返回 None，不通过返回原因（会带回给模型）。"""
    if not evidence:
        return "没有给出证据位置"
    texts = []
    for ev in evidence:
        try:
            s, e = int(ev["start_line"]), int(ev["end_line"])
        except (KeyError, TypeError, ValueError):
            return f"证据格式不对: {ev}"
        if e - s > 200:
            return f"{ev['path']} 的证据范围太大（{s}-{e}），请缩小到具体方法"
        t = snippet(sandbox, ev)
        if t is None:
            return f"{ev.get('path')} 第 {s}-{e} 行不存在"
        texts.append(t)
    joined = "\n".join(texts)
    wanted = [i.lstrip("@") for i in idents if i] or [i.lstrip("@") for i in identifiers(fallback_text) if code_like(i)]
    if not wanted:
        return None
    if not any(w and w in joined for w in wanted):
        return f"证据片段里没有出现 {', '.join(wanted[:5])}"
    return None
