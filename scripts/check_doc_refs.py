"""检查学习文档里的 `文件:行号` 引用（ROADMAP B0-13）。

为什么要有它：学习文档靠"文件:行号"把知识点落到代码上，可代码一改，行号就跑偏，
而且没人会发现（learn/M3.md 里 graph.py 的引用一天之内就整体偏了 3 行）。

约定的写法：引用后面紧跟函数名（或类名、常量名），都用反引号：
    `backend/app/sessions/graph.py:163` `select_question`
    `:197` `ask`            ← 只写 :行号 的，沿用前面最近一次出现的文件
检查三件事：文件存在；行号不越界；函数名就在那一行上下两行之内。

用法（在仓库根目录）：
    python scripts/check_doc_refs.py            # 检查，发现问题就以非 0 退出（CI 用）
    python scripts/check_doc_refs.py --fix      # 一次性修复：补函数名、按函数名改正行号
只用标准库，CI 里不需要装依赖。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted((ROOT / "docs" / "learn").glob("*.md"))
SPAN = re.compile(r"`([^`]+)`")
FULL = re.compile(r"^((?:backend|frontend|deploy)/[^\s:`]+\.\w+):(\d+)$")
SHORT = re.compile(r"^:(\d+)$")
NAME = re.compile(r"^[A-Za-z_][\w.]*$")
# 推断名字时认的"定义行"：Python 的 def/class/赋值，TS/Vue 的 function/const
DEF_LINE = re.compile(
    r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)|^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"
    r"|^\s*(?:export\s+)?const\s+(\w+)|^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)"
)
WINDOW = 2


@dataclass
class Ref:
    doc: Path
    lineno: int  # 文档里的行号
    path: str
    line: int
    name: str | None
    raw: str  # 反引号里的原文（完整引用或 :行号），修复时原样替换
    start: int  # 这个反引号片段在文档行里的位置
    end: int


def _lines(path: str) -> list[str] | None:
    p = ROOT / path
    return p.read_text(encoding="utf-8").splitlines() if p.is_file() else None


def collect(doc: Path) -> list[Ref]:
    refs: list[Ref] = []
    last_file: str | None = None
    for i, text in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        spans = list(SPAN.finditer(text))
        for k, m in enumerate(spans):
            full, short = FULL.match(m.group(1)), SHORT.match(m.group(1))
            if full:
                path, line = full.group(1), int(full.group(2))
                last_file = path
            elif short and last_file:
                path, line = last_file, int(short.group(1))
            else:
                continue
            name = None
            if k + 1 < len(spans):
                nxt = spans[k + 1]
                between = text[m.end() : nxt.start()]
                if not between.strip() and NAME.match(nxt.group(1)) and not FULL.match(nxt.group(1)):
                    name = nxt.group(1)
            refs.append(Ref(doc, i, path, line, name, m.group(1), m.start(), m.end()))
    return refs


def _near(lines: list[str], line: int, name: str) -> bool:
    """名字在这一行出现，或者它的定义在上下两行之内。

    只要求"附近出现这个词"太松：graph.py 里 route_after_answer 的返回值就有 "assess"，
    偏了 3 行的 assess 引用也能蒙混过去。
    """
    short = re.escape(name.split(".")[-1].rstrip("()"))
    # 字符串里的同名词不算（route_after_answer 里的 "assess" 是路由名，不是函数）
    code = re.sub(r"([\"']).*?\1", "", lines[line - 1])
    if re.search(rf"\b{short}\b", code):
        return True
    define = re.compile(
        rf"^\s*(?:async\s+)?def\s+{short}\b|^\s*class\s+{short}\b|^\s*(?:export\s+)?(?:async\s+)?function\s+{short}\b"
        rf"|^\s*(?:export\s+)?const\s+{short}\b|^\s*{short}\s*(?::[^=]+)?=(?!=)"
    )
    lo, hi = max(1, line - WINDOW), min(len(lines), line + WINDOW)
    return any(define.search(lines[n - 1]) for n in range(lo, hi + 1))


def check(refs: list[Ref]) -> list[str]:
    errors = []
    for r in refs:
        where = f"{r.doc.relative_to(ROOT)}:{r.lineno}"
        lines = _lines(r.path)
        if lines is None:
            errors.append(f"{where}: 文件不存在 {r.path}")
        elif not 1 <= r.line <= len(lines):
            errors.append(f"{where}: {r.path}:{r.line} 越界（文件共 {len(lines)} 行）")
        elif r.name is None:
            errors.append(f"{where}: {r.path}:{r.line} 后面没写函数名")
        elif not _near(lines, r.line, r.name):
            errors.append(f"{where}: {r.path}:{r.line} 附近找不到 {r.name}")
    return errors


def _definition(lines: list[str], name: str) -> int | None:
    """在文件里找 name 的定义行；不唯一或找不到返回 None。"""
    short = re.escape(name.split(".")[-1].rstrip("()"))
    pat = re.compile(
        rf"^\s*(?:async\s+)?def\s+{short}\b|^\s*class\s+{short}\b|^\s*(?:export\s+)?(?:async\s+)?function\s+{short}\b"
        rf"|^\s*(?:export\s+)?const\s+{short}\b|^\s*{short}\s*(?::[^=]+)?=(?!=)"
    )
    hits = [i for i, ln in enumerate(lines, 1) if pat.search(ln)]
    return hits[0] if len(hits) == 1 else None


ENCLOSING = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)|^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")


def _infer_name(lines: list[str], line: int) -> tuple[str | None, bool]:
    """那一行是定义行就取它的名字（可信）；否则取上方最近的函数或类（可疑：引用可能本来就偏了）。"""
    m = DEF_LINE.search(lines[line - 1])
    if m:
        return next(g for g in m.groups() if g), True
    for n in range(line - 1, 0, -1):
        m = ENCLOSING.search(lines[n - 1])
        if m:
            return next(g for g in m.groups() if g), False
    return None, False


def fix(refs: list[Ref]) -> list[str]:
    """按函数名改正行号；没写函数名的按那一行推断并补上。返回需要人工看的地方。"""
    review: list[str] = []
    edits: dict[Path, dict[int, list[tuple[int, int, str]]]] = {}
    for r in refs:
        lines = _lines(r.path)
        if lines is None:
            review.append(f"{r.doc.name}:{r.lineno} 文件不存在 {r.path}")
            continue
        old = f"`{r.raw}`"
        if r.name:
            if 1 <= r.line <= len(lines) and _near(lines, r.line, r.name):
                continue
            new_line = _definition(lines, r.name)
            if new_line is None:
                review.append(f"{r.doc.name}:{r.lineno} 找不到 {r.name} 的唯一定义（{r.path}:{r.line}）")
                continue
            new = old.replace(f":{r.line}`", f":{new_line}`")
        else:
            if not 1 <= r.line <= len(lines):
                review.append(f"{r.doc.name}:{r.lineno} 越界 {r.path}:{r.line}")
                continue
            name, sure = _infer_name(lines, r.line)
            if name is None:
                review.append(f"{r.doc.name}:{r.lineno} 推断不出名字 {r.path}:{r.line}")
                continue
            if not sure:
                review.append(f"{r.doc.name}:{r.lineno} {r.path}:{r.line} 不是定义行，按上方的 {name} 补了名字，请人工确认")
            new = f"{old} `{name}`"
        edits.setdefault(r.doc, {}).setdefault(r.lineno, []).append((r.start, r.end, new))
    for doc, by_line in edits.items():
        text = doc.read_text(encoding="utf-8").split("\n")
        for lineno, changes in by_line.items():
            s = text[lineno - 1]
            for start, end, new in sorted(changes, reverse=True):
                s = s[:start] + new + s[end:]
            text[lineno - 1] = s
        doc.write_bytes("\n".join(text).encode("utf-8"))
    return review


def main() -> int:
    refs = [r for d in DOCS for r in collect(d)]
    if "--fix" in sys.argv:
        for item in fix(refs):
            print(item)
        refs = [r for d in DOCS for r in collect(d)]
    errors = check(refs)
    for e in errors:
        print(e)
    print(f"检查了 {len(refs)} 处引用，{len(errors)} 处有问题")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
