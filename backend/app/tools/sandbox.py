"""Agent 可调用的只读工具 + 沙箱（SPEC 8.1）。

所有"读仓库"的操作都必须经过这里（SPEC 5.3、18.2）：
- 路径解析后必须落在快照根目录内：拒绝 ..、绝对路径、指向根目录外的符号链接；
- 拒绝敏感文件（.env*、*.pem、*.key、id_rsa*、*.jks……），grep 也会排除它们；
- 输出超长就截断并注明；
- 每次调用写 agent_steps（工具名、参数、耗时、结果摘要）。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import AppError
from app.core.text import truncate
from app.llm.types import CallContext, ToolSpec
from app.models import CodeRef, CodeSymbol, RepoProfile
from app.ops.trace import record_step

SENSITIVE_PATTERNS = [
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.crt",
    "credentials*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "secrets.*",
    "*secret*.yml",
    "*secret*.yaml",
    "*.kdbx",
]
SKIP_DIRS = {
    ".git",
    "node_modules",
    "target",
    "build",
    "dist",
    "out",
    ".gradle",
    ".idea",
    ".mvn",
    "generated",
    "generated-sources",
    ".venv",
    "__pycache__",
    "vendor",
}
MAX_OUTPUT = 8000
MAX_READ_LINES = 300


class SandboxError(AppError):
    code = "sandbox_denied"
    status_code = 403


def is_sensitive(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return any(fnmatch.fnmatch(name, p) for p in SENSITIVE_PATTERNS)


ToolHook = Callable[[str, dict[str, Any]], Awaitable[None]]


class Sandbox:
    def __init__(
        self,
        root: Path | str,
        snapshot_id: uuid.UUID | None = None,
        *,
        ctx: CallContext | None = None,
        kb_user_id: uuid.UUID | None = None,
        allow_kb: bool = False,
        allow_vector: bool = False,
        on_tool: ToolHook | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.snapshot_id = snapshot_id
        self.ctx = ctx
        self.kb_user_id = kb_user_id
        self.allow_kb = allow_kb
        self.allow_vector = allow_vector
        self.on_tool = on_tool

    # ------------------------------------------------------------ 路径校验
    def resolve(self, rel: str) -> Path:
        rel = (rel or ".").replace("\\", "/").strip()
        if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
            raise SandboxError("只允许仓库内的相对路径")
        parts = PurePosixPath(rel).parts
        if ".." in parts:
            raise SandboxError("路径中不允许出现 ..")
        if any(p in (".git",) for p in parts):
            raise SandboxError("不允许访问 .git 目录")
        p = (self.root / rel).resolve()
        # resolve() 会跟随符号链接，所以这里能拦住"链接指向根目录外"的情况
        if p != self.root and self.root not in p.parents:
            raise SandboxError("路径越出了仓库根目录")
        if p.is_file() and is_sensitive(p.name):
            raise SandboxError(f"拒绝读取敏感文件: {PurePosixPath(rel).name}")
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)).replace("\\", "/")

    # ------------------------------------------------------------ 工具实现
    async def list_dir(self, path: str = ".") -> str:
        p = self.resolve(path)
        if not p.is_dir():
            raise SandboxError("不是目录")
        items = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
            if child.name in SKIP_DIRS or (child.is_file() and is_sensitive(child.name)):
                continue
            items.append(child.name + ("/" if child.is_dir() else ""))
        return "\n".join(items[:500]) or "(空目录)"

    async def glob(self, pattern: str) -> str:
        if ".." in pattern or pattern.startswith("/"):
            raise SandboxError("模式里不允许 .. 或绝对路径")
        out: list[str] = []
        for p in self.root.glob(pattern):
            if len(out) >= 200:
                out.append("…[最多返回 200 条]")
                break
            rel = self.rel(p)
            if any(part in SKIP_DIRS for part in PurePosixPath(rel).parts) or is_sensitive(p.name) or p.is_symlink():
                continue
            if p.is_file():
                out.append(rel)
        return "\n".join(out) or "(没有匹配的文件)"

    async def grep(self, pattern: str, path_glob: str | None = None) -> str:
        args = [
            "rg",
            "-n",
            "--no-heading",
            "--color",
            "never",
            "--max-filesize",
            "512K",
            "-M",
            "300",
            "--max-columns-preview",
        ]
        for pat in SENSITIVE_PATTERNS:
            args += ["-g", f"!{pat}"]
        for d in SKIP_DIRS:
            args += ["-g", f"!{d}/"]
        if path_glob:
            if ".." in path_glob:
                raise SandboxError("path_glob 里不允许 ..")
            args += ["-g", path_glob]
        args += ["-e", pattern, "."]
        out = await self._rg(args)
        if out is None:  # 正则不合法 → 按字面量再试一次
            args[args.index("-e")] = "-F"
            out = await self._rg(args) or ""
        lines = [ln.removeprefix("./") for ln in out.splitlines() if ln.strip()]
        if len(lines) > 100:
            lines = lines[:100] + [f"…[共 {len(lines)} 条匹配，只显示前 100 条]"]
        return "\n".join(lines) or "(没有匹配)"

    async def _rg(self, args: list[str]) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(self.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        except TimeoutError:
            proc.kill()
            return "(搜索超时)"
        if proc.returncode == 2 and b"regex" in err.lower():
            return None
        return out.decode(errors="replace")

    async def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise SandboxError("文件不存在")
        start = max(1, int(start or 1))
        end = int(end) if end else start + MAX_READ_LINES - 1
        end = min(end, start + MAX_READ_LINES - 1)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[start - 1 : end]
        body = "\n".join(f"{i}: {line}" for i, line in enumerate(chunk, start))
        more = f"\n…[文件共 {len(lines)} 行]" if end < len(lines) else ""
        return f"{self.rel(p)} 第 {start}-{min(end, len(lines))} 行\n{body}{more}"

    def read_lines_raw(self, path: str, start: int, end: int) -> list[str] | None:
        """内部用（证据校验、阅卷取片段）：同样走路径校验，返回原始行；越界返回 None。"""
        try:
            p = self.resolve(path)
        except SandboxError:
            return None
        if not p.is_file():
            return None
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if start < 1 or end < start or end > len(lines):
            return None
        return lines[start - 1 : end]

    def read_text_raw(self, path: str) -> str | None:
        try:
            p = self.resolve(path)
        except SandboxError:
            return None
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None

    async def find_symbol(self, name: str) -> str:
        if self.snapshot_id is None:
            return "(没有符号索引)"
        async with session_scope() as db:
            from app.models import CodeFile

            rows = (
                await db.execute(
                    select(CodeSymbol, CodeFile.path)
                    .join(CodeFile, CodeFile.id == CodeSymbol.file_id)
                    .where(CodeSymbol.snapshot_id == self.snapshot_id)
                    .where(
                        or_(
                            CodeSymbol.name == name,
                            CodeSymbol.qualified_name == name,
                            CodeSymbol.qualified_name.like(f"%.{name}"),
                        )
                    )
                    .limit(30)
                )
            ).all()
        if not rows:
            return f"(没有找到符号 {name})"
        return "\n".join(
            f"{s.kind} {s.qualified_name} @ {path}:{s.start_line}-{s.end_line}  {s.signature or ''}" for s, path in rows
        )

    async def find_references(self, symbol: str) -> str:
        if self.snapshot_id is None:
            return "(没有符号索引)"
        name = symbol.rsplit(".", 1)[-1]
        async with session_scope() as db:
            from app.models import CodeFile

            caller = CodeSymbol.__table__.alias("caller")
            rows = (
                await db.execute(
                    select(CodeRef.line, CodeRef.receiver, caller.c.qualified_name, CodeFile.path)
                    .join(caller, caller.c.id == CodeRef.from_symbol_id)
                    .join(CodeFile, CodeFile.id == caller.c.file_id)
                    .where(CodeRef.snapshot_id == self.snapshot_id, CodeRef.to_name == name)
                    .limit(50)
                )
            ).all()
        if not rows:
            return f"(没有找到对 {name} 的引用)"
        return "\n".join(
            f"{path}:{line} 在 {q} 中调用 {(recv + '.') if recv else ''}{name}" for line, recv, q, path in rows
        )

    async def _profile(self) -> dict[str, Any]:
        if self.snapshot_id is None:
            return {}
        async with session_scope() as db:
            prof = await db.get(RepoProfile, self.snapshot_id)
        return prof.profile if prof else {}

    async def get_profile(self, section: str = "summary") -> str:
        prof = await self._profile()
        if section in ("summary", "", None):
            keys = {k: (len(v) if isinstance(v, list | dict) else v) for k, v in prof.items()}
            return json.dumps({"stack": prof.get("stack", {}).get("tech"), "sections": keys}, ensure_ascii=False)
        if section.startswith("usages.") and "usages" in prof:
            return json.dumps(prof["usages"].get(section.split(".", 1)[1], []), ensure_ascii=False)
        return json.dumps(prof.get(section, f"(没有 {section} 这一部分)"), ensure_ascii=False)

    async def get_chain(self, endpoint: str) -> str:
        prof = await self._profile()
        for ch in prof.get("chains", []):
            if ch["endpoint"] == endpoint or endpoint in ch["endpoint"]:
                return "\n".join(
                    f"{'  ' * s['depth']}[{s['layer']}] {s['symbol']} @ {s['file']}:{s['line']}" for s in ch["steps"]
                )
        return f"(没有找到接口 {endpoint} 的链路)"

    async def search_kb(self, query: str) -> str:
        if not self.allow_kb or self.kb_user_id is None:
            raise SandboxError("当前模式不开放知识库检索")
        from app.kb.service import search

        hits = await search(self.kb_user_id, query, top_k=5)
        return (
            "\n\n".join(f"[{h['title']} / {h['heading_path']}]\n{h['content'][:600]}" for h in hits)
            or "(知识库没有结果)"
        )

    async def vector_search(self, query: str) -> str:
        if not self.allow_vector or self.snapshot_id is None:
            raise SandboxError("当前检索模式不开放向量检索")
        from app.retrieval.vector import vector_search

        hits = await vector_search(self.snapshot_id, query, top_k=8)
        return "\n".join(f"{h.path}:{h.start_line}-{h.end_line} (score={h.score:.3f})" for h in hits) or "(没有结果)"

    # ------------------------------------------------------------ 统一调度
    def specs(self) -> list[ToolSpec]:
        specs = [s for s in TOOL_SPECS if s.name not in ("search_kb", "vector_search")]
        if self.allow_kb:
            specs.append(next(s for s in TOOL_SPECS if s.name == "search_kb"))
        if self.allow_vector:
            specs.append(next(s for s in TOOL_SPECS if s.name == "vector_search"))
        return specs

    async def call(self, name: str, args: dict[str, Any], node: str = "tool") -> str:
        fn = {
            "list_dir": self.list_dir,
            "glob": self.glob,
            "grep": self.grep,
            "read_file": self.read_file,
            "find_symbol": self.find_symbol,
            "find_references": self.find_references,
            "get_profile": self.get_profile,
            "get_chain": self.get_chain,
            "search_kb": self.search_kb,
            "vector_search": self.vector_search,
        }.get(name)
        t0 = time.perf_counter()
        if self.on_tool is not None:
            await self.on_tool(name, args)
        if fn is None:
            result = f"(未知工具 {name})"
        else:
            try:
                result = await fn(**args)
            except SandboxError as e:
                result = f"(拒绝：{e.message})"
            except TypeError as e:
                result = f"(参数错误：{e})"
        result = truncate(result, MAX_OUTPUT)
        await record_step(
            self.ctx,
            node=node,
            tool_name=name,
            tool_args=args,
            result_preview=result,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        return result


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "list_dir",
        "列出仓库内某个目录的内容",
        {"type": "object", "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}}},
    ),
    ToolSpec(
        "glob",
        "按 glob 模式找文件，最多返回 200 条",
        {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "如 **/*Service*.java"}},
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        "grep",
        "用 ripgrep 搜索代码，最多 100 条匹配，每条带行号",
        {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path_glob": {"type": "string"}},
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        "read_file",
        "读取文件的指定行，一次最多 300 行",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
            "required": ["path"],
        },
    ),
    ToolSpec(
        "find_symbol",
        "按名字查类、方法、字段的定义位置",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    ),
    ToolSpec(
        "find_references",
        "查某个方法的近似调用位置",
        {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    ),
    ToolSpec(
        "get_profile",
        "读取仓库画像的某一部分：summary/stack/endpoints/components/usages.transaction 等",
        {"type": "object", "properties": {"section": {"type": "string"}}},
    ),
    ToolSpec(
        "get_chain",
        "读取某个接口的调用链路，如 'POST /api/feed/{id}/like'",
        {"type": "object", "properties": {"endpoint": {"type": "string"}}, "required": ["endpoint"]},
    ),
    ToolSpec(
        "search_kb",
        "检索用户的八股知识库",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    ToolSpec(
        "vector_search",
        "代码向量检索，返回最相近的代码块位置",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
]


def snapshot_root(repository_id: uuid.UUID | str, sha: str) -> Path:
    return Path(get_settings().repo_storage_dir) / str(repository_id) / sha
