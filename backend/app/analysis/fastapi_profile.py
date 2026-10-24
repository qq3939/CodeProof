"""FastAPI 画像：只抽路由和依赖注入（SPEC 7.4.2、D-022）。

接口的格式和 Spring 的 extract_endpoints 一样，后面的链路、候选考点不用区分框架。
完整路径 = include_router 的前缀 + APIRouter 的前缀 + 装饰器里的路径。

include_router 的第一个参数解析不出是哪个路由器时（本项目的 main.py 就是在循环里
include_router(r, prefix="/api")），把这些调用的前缀当作"默认前缀"：前缀都一样才用，
不一样就不猜，宁可路径少一段也不要拼错。
"""

from __future__ import annotations

import re
from typing import Any

from app.analysis.code_model import CodeModel, FileEntry, SymRef

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}


def _join(*parts: str) -> str:
    full = "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return full


def _first_string(args: str) -> str | None:
    m = re.match(r"\s*[rbuf]*([\"'])(.*?)\1", args)
    return m.group(2) if m else None


def _resolve_router(f: FileEntry, text: str, routers: dict[tuple[str, str], dict[str, Any]]) -> tuple[str, str] | None:
    """include_router 的参数 → (模块, 路由器变量名)。"""
    idx = f.index
    if "." in text:
        base, var = text.rsplit(".", 1)
        mod = idx.aliases.get(base)
        return (mod, var) if mod and (mod, var) in routers else None
    if text in idx.aliases:
        mod, var = idx.aliases[text].rsplit(".", 1)
        return (mod, var) if (mod, var) in routers else None
    return (idx.package, text) if (idx.package, text) in routers else None


def _dependency_symbol(model: CodeModel, f: FileEntry, name: str) -> SymRef | None:
    """Depends(get_db) 里的 get_db → 仓库里的函数：先找本模块，再按导入找。"""
    short = name.rsplit(".", 1)[-1]
    cands = [m for m in model.methods_by_name.get(short, []) if m.sym.kind in ("function", "method")]
    same = [m for m in cands if m.file is f]
    if same:
        return same[0]
    full = f.index.aliases.get(name) or f.index.aliases.get(name.split(".", 1)[0])
    for m in cands:
        if full and (model.qualified(m) == full or full.endswith(f".{m.sym.name}")):
            return m
    return cands[0] if len(cands) == 1 else None


def extract_fastapi_endpoints(model: CodeModel) -> list[dict[str, Any]]:
    py_files = [f for f in model.files if f.index.language == "python"]
    routers: dict[tuple[str, str], dict[str, Any]] = {}
    for f in py_files:
        for var, r in f.index.extras.get("routers", {}).items():
            routers[(f.index.package, var)] = r
    if not routers:
        return []
    include_prefix: dict[tuple[str, str], str] = {}
    unresolved: set[str] = set()
    for f in py_files:
        for inc in f.index.extras.get("include_routers", []):
            key = _resolve_router(f, inc["router"], routers)
            if key is None:
                unresolved.add(inc["prefix"])
            else:
                include_prefix.setdefault(key, inc["prefix"])
    default_prefix = next(iter(unresolved)) if len(unresolved) == 1 else ""

    out: list[dict[str, Any]] = []
    for f in py_files:
        deps_by_symbol: dict[int, list[str]] = {}
        for d in f.index.extras.get("depends", []):
            deps_by_symbol.setdefault(d["symbol"], []).append(d["name"])
        for i, sid in enumerate(f.symbol_ids):
            m = model.by_id[sid]
            if m.sym.kind not in ("function", "method"):
                continue
            for a in m.sym.annotations:
                var, _, verb = a.name.rpartition(".")
                key = (f.index.package, var)
                if key not in routers or verb not in HTTP_METHODS | {"api_route"}:
                    continue
                r = routers[key]
                prefix = include_prefix.get(key, default_prefix if r["kind"] == "APIRouter" else "")
                path = _join(prefix, r["prefix"], _first_string(a.args) or "")
                if verb == "api_route":
                    listed = re.search(r"methods\s*=\s*\[([^\]]*)\]", a.args)
                    methods = re.findall(r"[\"'](\w+)[\"']", listed.group(1)) if listed else ["ANY"]
                else:
                    methods = [verb.upper()]
                # 依赖：参数里的 Depends、装饰器里的 dependencies=[Depends(...)]、路由器上的 dependencies
                deps = [*deps_by_symbol.get(i, []), *re.findall(r"Depends\(\s*([\w.]+)", a.args), *r["dependencies"]]
                dep_refs = []
                for name in dict.fromkeys(deps):
                    ref = _dependency_symbol(model, f, name)
                    dep_refs.append(
                        {"name": name, "symbol_id": str(ref.id) if ref else None, "file": ref.path if ref else None}
                    )
                for hm in methods:
                    out.append(
                        {
                            "method": hm,
                            "path": path,
                            "handler": m.short,
                            "symbol_id": str(m.id),
                            "file": m.path,
                            "line": m.sym.start_line,
                            "end_line": m.sym.end_line,
                            "framework": "fastapi",
                            "depends": dep_refs,
                        }
                    )
    out.sort(key=lambda e: (e["path"], e["method"]))
    return out
