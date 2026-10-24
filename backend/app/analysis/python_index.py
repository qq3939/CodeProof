"""Python 符号抽取（tree-sitter-python）。SPEC 7.3、D-022。

输出和 Java 一样的 FileIndex（见 lang_index）：类、函数、方法、装饰器、调用。
和 Java 的差别：
- Python 没有包声明，模块名按文件路径推出来（app/sessions/graph.py → app.sessions.graph）；
- 调本类方法必须写 self.，所以 self.f() 的接收者类型就是所在的类；裸调用 f() 只可能是函数；
- 导入可以起别名、可以写在函数里（本项目的 main.py 就是），一律收进 aliases，解析 mod.f() 时用；
- FastAPI 只抽路由器、include_router 和 Depends，放进 extras，由 fastapi_profile 拼出接口。

为什么不用 Python 自带的 ast：ast 遇到一处语法错误整个文件都解析不了，也只认当前解释器版本的语法；
tree-sitter 容错，和 Java 那边是同一套做法。这个文件同样是纯函数，不碰数据库。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from app.analysis.lang_index import Annotation, Call, FileIndex, Symbol

PY = Language(tspython.language())

ROUTER_FACTORIES = {"APIRouter": "APIRouter", "FastAPI": "FastAPI"}
# 内置函数的调用不进调用关系：数量多、解析不到仓库里的符号，只会让"已解析调用"的比例失真
BUILTINS = {
    "print",
    "len",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "range",
    "enumerate",
    "zip",
    "isinstance",
    "issubclass",
    "getattr",
    "setattr",
    "hasattr",
    "super",
    "type",
    "min",
    "max",
    "sum",
    "any",
    "all",
    "sorted",
    "reversed",
    "map",
    "filter",
    "open",
    "repr",
    "abs",
    "round",
    "next",
    "iter",
    "id",
    "hash",
}


ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")


def source_roots(paths: list[str]) -> list[str]:
    """Python 包的根目录：有 pyproject.toml / setup.py / setup.cfg / requirements*.txt 的目录。

    很多仓库的 Python 代码不在仓库根目录（本项目就在 backend/ 下）。模块名要相对这个根算，
    否则 backend/app/x.py 会被叫成 backend.app.x，和代码里写的 import app.x 对不上，跨模块的调用全都解析不到。
    """
    roots = set()
    for p in paths:
        pp = PurePosixPath(p)
        if pp.name in ROOT_MARKERS or (pp.name.startswith("requirements") and pp.suffix == ".txt"):
            roots.add("" if str(pp.parent) == "." else str(pp.parent))
    # 长的在前：嵌套的根（如 services/api/）优先于外层
    return sorted(roots, key=lambda r: (-len(r), r))


def relative_to_root(path: str, roots: list[str]) -> str:
    """把文件路径换成相对所属根目录的路径，再交给 module_name。没有根就按仓库根算。"""
    for r in roots:
        if r and path.startswith(r + "/"):
            return path[len(r) + 1 :]
    return path


def module_name(path: str) -> str:
    """app/sessions/graph.py → app.sessions.graph；包的 __init__.py 就是包名；src 布局去掉 src。"""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _text(node: Node | None, src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _simple_type(t: str) -> str:
    """Optional[Foo] / Foo | None / Annotated[Foo, ...] / list[Foo] → 取最可能是"那个对象"的类型简单名。"""
    t = t.strip().strip("\"'")
    t = t.split("|")[0].strip() if "|" in t and not t.startswith(("Optional", "Annotated")) else t
    m = re.match(r"^(?:typing\.)?(Optional|Annotated)\[\s*([^,\]]+)", t)
    if m:
        t = m.group(2)
    t = re.sub(r"\[.*\]", "", t).strip()
    return t.rsplit(".", 1)[-1]


def _string_value(node: Node | None, src: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    return "".join(_text(c, src) for c in node.children if c.type == "string_content")


def _resolve_relative(module: str, is_package: bool, dots: int, name: str) -> str:
    """from .x import y：以当前模块所在的包为起点，每多一个点往上一层。"""
    base = module.split(".") if is_package else module.split(".")[:-1]
    if dots > 1:
        base = base[: max(0, len(base) - (dots - 1))]
    return ".".join([*base, name] if name else base)


def _decorators(node: Node, src: bytes) -> list[Annotation]:
    """decorated_definition 下的装饰器：@router.post("/x") → name=router.post, args="/x"。"""
    out: list[Annotation] = []
    if node.parent is None or node.parent.type != "decorated_definition":
        return out
    for d in node.parent.children:
        if d.type != "decorator":
            continue
        expr = next((c for c in d.children if c.type != "@"), None)
        if expr is None:
            continue
        if expr.type == "call":
            name = _text(expr.child_by_field_name("function"), src)
            args = _text(expr.child_by_field_name("arguments"), src).strip()
            if args.startswith("(") and args.endswith(")"):
                args = args[1:-1].strip()
        else:
            name, args = _text(expr, src), ""
        out.append(Annotation(name=re.sub(r"\s+", "", name), args=args, line=_line(d)))
    return out


def _call_name(call: Node, src: bytes) -> str:
    return _text(call.child_by_field_name("function"), src)


def _depends_in(node: Node, src: bytes) -> list[str]:
    """一个参数（或 dependencies=[...]）里所有 Depends(x) 的 x。"""
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "call" and _call_name(n, src).rsplit(".", 1)[-1] == "Depends":
            args = n.child_by_field_name("arguments")
            first = next((c for c in args.children if c.type in ("identifier", "attribute")), None) if args else None
            if first is not None:
                out.append(_text(first, src))
            continue
        stack.extend(n.children)
    return out


def _kwargs(args: Node | None, src: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    if args is None:
        return out
    for c in args.children:
        if c.type == "keyword_argument":
            k, v = c.child_by_field_name("name"), c.child_by_field_name("value")
            if k is not None and v is not None:
                out[_text(k, src)] = v
    return out


def parse_python(source: bytes, path: str) -> FileIndex:
    tree = Parser(PY).parse(source)
    module = module_name(path)
    is_package = PurePosixPath(path).name == "__init__.py"
    imports: list[str] = []
    aliases: dict[str, str] = {}
    symbols: list[Symbol] = []
    calls: list[Call] = []
    routers: dict[str, dict[str, Any]] = {}
    includes: list[dict[str, Any]] = []
    depends: list[dict[str, Any]] = []
    # 类下标 → 属性名 → 类型（self.x = Foo()、x: Foo），解析 self.x.f() 用
    field_types: dict[int, dict[str, str]] = {}

    def add_import(full: str, local: str) -> None:
        if full not in imports:
            imports.append(full)
        aliases[local] = full

    def visit_import(n: Node) -> None:
        if n.type == "import_statement":
            for c in n.children_by_field_name("name"):
                if c.type == "aliased_import":
                    add_import(
                        _text(c.child_by_field_name("name"), source), _text(c.child_by_field_name("alias"), source)
                    )
                else:
                    add_import(_text(c, source), _text(c, source))
            return
        mod_node = n.child_by_field_name("module_name")
        if mod_node is None:
            return
        if mod_node.type == "relative_import":
            prefix = next((c for c in mod_node.children if c.type == "import_prefix"), None)
            dots = _text(prefix, source).count(".") if prefix is not None else 1
            name = next((_text(c, source) for c in mod_node.children if c.type == "dotted_name"), "")
            base = _resolve_relative(module, is_package, dots, name)
        else:
            base = _text(mod_node, source)
        for c in n.children_by_field_name("name"):
            if c.type == "aliased_import":
                n_ = _text(c.child_by_field_name("name"), source)
                add_import(f"{base}.{n_}", _text(c.child_by_field_name("alias"), source))
            else:
                add_import(f"{base}.{_text(c, source)}", _text(c, source))

    def record_router(target: Node, value: Node) -> None:
        """x = APIRouter(prefix="/ops", dependencies=[Depends(require_admin)]) / app = FastAPI()。"""
        if value.type != "call" or target.type != "identifier":
            return
        kind = ROUTER_FACTORIES.get(_call_name(value, source).rsplit(".", 1)[-1])
        if kind is None:
            return
        kw = _kwargs(value.child_by_field_name("arguments"), source)
        routers[_text(target, source)] = {
            "kind": kind,
            "prefix": _string_value(kw.get("prefix"), source) or "",
            "dependencies": _depends_in(kw["dependencies"], source) if "dependencies" in kw else [],
            "line": _line(value),
        }

    def record_include(call: Node) -> None:
        args = call.child_by_field_name("arguments")
        if args is None:
            return
        first = next((c for c in args.children if c.type in ("identifier", "attribute")), None)
        if first is None:
            return
        prefix = _string_value(_kwargs(args, source).get("prefix"), source) or ""
        includes.append({"router": _text(first, source), "prefix": prefix, "line": _line(call)})

    def resolve_receiver(recv: str | None, cls: int | None, locals_: dict[str, str]) -> str | None:
        if recv is None:
            return None
        if recv in ("self", "cls"):
            return symbols[cls].name if cls is not None else None
        if recv.startswith(("self.", "cls.")) and cls is not None:
            attr = recv.split(".", 1)[1]
            return field_types.get(cls, {}).get(attr)
        if recv in locals_:
            return locals_[recv]
        if recv in aliases:
            return None  # 模块别名：交给代码模型按 aliases 去对应模块里找
        if re.fullmatch(r"[A-Z][A-Za-z0-9_]*", recv):
            return recv  # 类名直接调用，如 OrderService.create()
        return None

    def note_assignment(n: Node, cls: int | None, locals_: dict[str, str]) -> None:
        left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
        typ = n.child_by_field_name("type")
        if left is None:
            return
        name = _text(left, source)
        t: str | None = None
        if typ is not None:
            t = _simple_type(_text(typ, source))
        elif right is not None and right.type == "call":
            fn = _call_name(right, source).rsplit(".", 1)[-1]
            if re.fullmatch(r"[A-Z][A-Za-z0-9_]*", fn):
                t = fn
        if right is not None:
            record_router(left, right)
        if not t:
            return
        if name.startswith("self.") and cls is not None:
            field_types.setdefault(cls, {})[name[5:]] = t
        elif left.type == "identifier":
            locals_[name] = t

    def collect_calls(body: Node, fidx: int, cls: int | None, locals_: dict[str, str]) -> None:
        # 迭代而不是递归：深的语法树会碰到 Python 的递归上限
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "class_definition"):
                visit_def(n, fidx, cls)  # 嵌套的函数、类单独成符号，它们里面的调用算它们自己的
                continue
            if n.type in ("import_statement", "import_from_statement"):
                visit_import(n)
            elif n.type == "assignment":
                note_assignment(n, cls, locals_)
            elif n.type == "call":
                fn = n.child_by_field_name("function")
                if fn is not None and fn.type == "attribute":
                    recv: str | None = _text(fn.child_by_field_name("object"), source)
                    name = _text(fn.child_by_field_name("attribute"), source)
                else:
                    recv, name = None, _text(fn, source)
                if name == "include_router":
                    record_include(n)
                if recv is not None:
                    # 链式调用的接收者可能跨好几行、很长：压缩空白再截断
                    recv = re.sub(r"\s+", " ", recv)[:120]
                chained = recv is not None and "(" in recv
                # 链式调用最左边的那个对象：self.llm.bind_tools(...).invoke() 的 self.llm
                root = re.match(r"((?:self|cls)\.[A-Za-z_]\w*|[A-Za-z_]\w*)", recv) if recv else None
                if name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and not (recv is None and name in BUILTINS):
                    calls.append(
                        Call(
                            from_symbol=fidx,
                            name=name,
                            receiver=recv,
                            receiver_type=None if chained else resolve_receiver(recv, cls, locals_),
                            line=_line(n),
                            root_type=resolve_receiver(root.group(1) if root else None, cls, locals_),
                        )
                    )
            stack.extend(reversed(n.children))

    def visit_def(node: Node, parent: int | None, cls: int | None) -> None:
        """函数或类。parent 是外层符号（类或函数），cls 是最近的外层类（解析 self 用）。"""
        name = _text(node.child_by_field_name("name"), source)
        prefix = symbols[parent].qualified_name if parent is not None else module
        qname = f"{prefix}.{name}" if prefix else name
        body = node.child_by_field_name("body")
        head_end = body.start_byte if body is not None else node.end_byte
        signature = re.sub(r"\s+", " ", source[node.start_byte : head_end].decode("utf-8", "replace")).strip()
        idx = len(symbols)
        if node.type == "class_definition":
            supers = []
            sup = node.child_by_field_name("superclasses")
            if sup is not None:
                supers = [_simple_type(_text(c, source)) for c in sup.children if c.type in ("identifier", "attribute")]
            symbols.append(
                Symbol(
                    kind="class",
                    name=name,
                    qualified_name=qname,
                    signature=signature.rstrip(":").strip(),
                    start_line=_line(node),
                    end_line=node.end_point[0] + 1,
                    annotations=_decorators(node, source),
                    parent=parent,
                    supertypes=supers,
                )
            )
            field_types[idx] = {}
            if body is not None:
                for member in body.children:
                    target = (
                        member.child_by_field_name("definition") if member.type == "decorated_definition" else member
                    )
                    if target is not None and target.type in ("function_definition", "class_definition"):
                        visit_def(target, idx, idx)
                    elif member.type == "expression_statement":
                        for a in member.children:
                            if a.type == "assignment":
                                # 类属性 x: Foo，或 x = Foo()：当作字段类型
                                note_assignment(a, idx, field_types[idx])
            return
        params: dict[str, str] = {}
        ps = node.child_by_field_name("parameters")
        if ps is not None:
            for p in ps.children:
                pname = p.child_by_field_name("name") if p.type != "identifier" else p
                if pname is None and p.children:
                    pname = p.children[0] if p.children[0].type == "identifier" else None
                ptype = p.child_by_field_name("type")
                if pname is not None and ptype is not None:
                    params[_text(pname, source)] = _simple_type(_text(ptype, source))
                for dep in _depends_in(p, source):
                    depends.append({"symbol": idx, "name": dep, "line": _line(p)})
        ret = node.child_by_field_name("return_type")
        symbols.append(
            Symbol(
                kind="method" if parent is not None and symbols[parent].kind == "class" else "function",
                name=name,
                qualified_name=qname,
                signature=signature.rstrip(":").strip(),
                start_line=_line(node),
                end_line=node.end_point[0] + 1,
                annotations=_decorators(node, source),
                parent=parent,
                type_text=_simple_type(_text(ret, source)) if ret is not None else None,
                params=params,
            )
        )
        if body is not None:
            collect_calls(body, idx, cls, dict(params))

    root = tree.root_node
    stack = list(reversed(root.children))
    while stack:
        n = stack.pop()
        if n.type == "decorated_definition":
            d = n.child_by_field_name("definition")
            if d is not None:
                visit_def(d, None, None)
        elif n.type in ("function_definition", "class_definition"):
            visit_def(n, None, None)
        elif n.type in ("import_statement", "import_from_statement"):
            visit_import(n)
        elif n.type == "assignment":
            note_assignment(n, None, {})
        elif n.type == "call" and _call_name(n, source).endswith(".include_router"):
            record_include(n)
        else:
            # 模块级的 if/try/with/表达式语句里也可能有导入、路由器和 include_router
            stack.extend(reversed(n.children))

    extras: dict[str, Any] = {}
    if routers:
        extras["routers"] = routers
    if includes:
        extras["include_routers"] = includes
    if depends:
        extras["depends"] = depends
    return FileIndex(
        language="python",
        package=module,
        imports=imports,
        symbols=symbols,
        calls=calls,
        aliases=aliases,
        extras=extras,
    )
