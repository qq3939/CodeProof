"""Java 符号抽取（tree-sitter）。SPEC 7.3。

为什么用 tree-sitter 而不是正则：注解、泛型、内部类、lambda 这些用正则几乎写不对；
tree-sitter 给出具体语法树，容错好（有语法错误的文件也能解析大部分）。
为什么不用 javac / JDT：那需要 JVM，也接近"构建"仓库，违背"不运行仓库代码"的边界。

这个文件是纯函数：输入源码字节，输出结构化结果（与语言无关的 FileIndex，见 lang_index），不碰数据库，方便单测。
"""

from __future__ import annotations

import re

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from app.analysis.lang_index import Annotation, Call, FileIndex, Symbol

JAVA = Language(tsjava.language())

TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}


def _text(node: Node | None, src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _simple_type(t: str) -> str:
    """List<UserService> → List；com.a.UserService → UserService；去掉数组和泛型。"""
    t = re.sub(r"<.*>", "", t).replace("[]", "").strip()
    return t.rsplit(".", 1)[-1]


def _annotations(node: Node, src: bytes) -> list[Annotation]:
    out: list[Annotation] = []
    for child in node.children:
        if child.type == "modifiers":
            for m in child.children:
                if m.type in ("marker_annotation", "annotation"):
                    name = _text(m.child_by_field_name("name"), src).rsplit(".", 1)[-1]
                    args = _text(m.child_by_field_name("arguments"), src).strip()
                    if args.startswith("(") and args.endswith(")"):
                        args = args[1:-1].strip()
                    out.append(Annotation(name=name, args=args, line=m.start_point[0] + 1))
    return out


def _signature(node: Node, src: bytes) -> str:
    """方法签名：去掉注解和方法体后的头部，压缩空白。"""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    head = src[node.start_byte : end].decode("utf-8", errors="replace")
    for child in node.children:
        if child.type == "modifiers":
            for m in child.children:
                if m.type in ("marker_annotation", "annotation"):
                    head = head.replace(_text(m, src), "", 1)
    return re.sub(r"\s+", " ", head).strip().rstrip(";").strip()


def _supertypes(node: Node, src: bytes) -> list[str]:
    out: list[str] = []
    for fname in ("superclass", "interfaces"):
        n = node.child_by_field_name(fname)
        if n is not None:
            txt = _text(n, src)
            txt = re.sub(r"^\s*(extends|implements)\s+", "", txt)
            out.extend(_simple_type(x) for x in re.split(r",(?![^<]*>)", txt) if x.strip())
    # 接口的 extends 在 tree-sitter-java 里是 extends_interfaces 子节点
    for child in node.children:
        if child.type == "extends_interfaces":
            txt = re.sub(r"^\s*extends\s+", "", _text(child, src))
            out.extend(_simple_type(x) for x in re.split(r",(?![^<]*>)", txt) if x.strip())
    return out


def _params(node: Node, src: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    params = node.child_by_field_name("parameters")
    if params is None:
        return out
    for p in params.children:
        if p.type in ("formal_parameter", "spread_parameter"):
            t = p.child_by_field_name("type")
            n = p.child_by_field_name("name")
            if n is None:
                # spread_parameter 的名字在 variable_declarator 里
                for c in p.children:
                    if c.type == "variable_declarator":
                        n = c.child_by_field_name("name")
            if t is not None and n is not None:
                out[_text(n, src)] = _simple_type(_text(t, src))
    return out


def parse_java(source: bytes) -> FileIndex:
    tree = Parser(JAVA).parse(source)
    root = tree.root_node
    package = ""
    imports: list[str] = []
    symbols: list[Symbol] = []
    calls: list[Call] = []
    sync_blocks: list[tuple[int, int]] = []

    for child in root.children:
        if child.type == "package_declaration":
            package = re.sub(r"^package\s+|;$", "", _text(child, source)).strip()
        elif child.type == "import_declaration":
            imports.append(re.sub(r"^import\s+(static\s+)?|;$", "", _text(child, source)).strip())

    # 每个类里 字段名 → 类型，用于解析 this.xxx / xxx 接收者
    field_types: dict[int, dict[str, str]] = {}

    def visit_type(node: Node, parent: int | None, prefix: str) -> None:
        kind = TYPE_KINDS[node.type]
        name = _text(node.child_by_field_name("name"), source)
        qname = f"{prefix}.{name}" if prefix else name
        idx = len(symbols)
        symbols.append(
            Symbol(
                kind=kind,
                name=name,
                qualified_name=qname,
                signature=re.sub(r"\s+", " ", _signature(node, source)),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                annotations=_annotations(node, source),
                parent=parent,
                supertypes=_supertypes(node, source),
            )
        )
        field_types[idx] = {}
        body = node.child_by_field_name("body")
        if body is None:
            return
        # record 的组件也当作字段
        if node.type == "record_declaration":
            for k, v in _params(node, source).items():
                field_types[idx][k] = v
        for member in body.children:
            if member.type in TYPE_KINDS:
                visit_type(member, idx, qname)
            elif member.type == "enum_body_declarations":
                for m2 in member.children:
                    visit_member(m2, idx, qname)
            else:
                visit_member(member, idx, qname)

    def visit_member(member: Node, cls: int, qname: str) -> None:
        if member.type in TYPE_KINDS:
            visit_type(member, cls, qname)
        elif member.type == "field_declaration":
            t = _simple_type(_text(member.child_by_field_name("type"), source))
            anns = _annotations(member, source)
            for d in member.children:
                if d.type == "variable_declarator":
                    fname = _text(d.child_by_field_name("name"), source)
                    field_types[cls][fname] = t
                    symbols.append(
                        Symbol(
                            kind="field",
                            name=fname,
                            qualified_name=f"{qname}.{fname}",
                            signature=re.sub(r"\s+", " ", _signature(member, source)),
                            start_line=member.start_point[0] + 1,
                            end_line=member.end_point[0] + 1,
                            annotations=anns,
                            parent=cls,
                            type_text=t,
                        )
                    )
        elif member.type in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
            name = _text(member.child_by_field_name("name"), source) or symbols[cls].name
            midx = len(symbols)
            params = _params(member, source)
            symbols.append(
                Symbol(
                    kind="constructor" if member.type != "method_declaration" else "method",
                    name=name,
                    qualified_name=f"{qname}.{name}",
                    signature=_signature(member, source),
                    start_line=member.start_point[0] + 1,
                    end_line=member.end_point[0] + 1,
                    annotations=_annotations(member, source),
                    parent=cls,
                    type_text=_simple_type(_text(member.child_by_field_name("type"), source)) or None,
                    params=params,
                )
            )
            body = member.child_by_field_name("body")
            if body is not None:
                locals_: dict[str, str] = dict(params)
                collect_calls(body, midx, cls, locals_)

    def resolve_receiver(recv: str | None, cls: int, locals_: dict[str, str]) -> str | None:
        if recv is None or recv == "this":
            return symbols[cls].name
        r = recv.removeprefix("this.")
        if r in locals_:
            return locals_[r]
        # 沿外部类往上找字段
        c: int | None = cls
        while c is not None:
            if r in field_types.get(c, {}):
                return field_types[c][r]
            c = symbols[c].parent
        if re.fullmatch(r"[A-Z][A-Za-z0-9_]*", r):
            return r  # 静态调用，如 Collections.sort
        return None

    def collect_calls(node: Node, midx: int, cls: int, locals_: dict[str, str]) -> None:
        # 迭代而不是递归：大方法的语法树可能很深，避免 Python 递归上限
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "local_variable_declaration":
                t = _simple_type(_text(n.child_by_field_name("type"), source))
                for d in n.children:
                    if d.type == "variable_declarator":
                        locals_[_text(d.child_by_field_name("name"), source)] = t
            elif n.type in ("enhanced_for_statement", "catch_formal_parameter"):
                t_node = n.child_by_field_name("type")
                name_node = n.child_by_field_name("name")
                if t_node is not None and name_node is not None:
                    locals_[_text(name_node, source)] = _simple_type(_text(t_node, source))
            elif n.type == "method_invocation":
                obj = n.child_by_field_name("object")
                recv = _text(obj, source) if obj is not None else None
                if recv is not None and len(recv) > 120:
                    recv = recv[:120]  # 链式调用的接收者可能很长
                chained = recv is not None and "(" in recv
                root = re.match(r"(?:this\.)?([A-Za-z_][A-Za-z0-9_]*)", recv) if recv else None
                calls.append(
                    Call(
                        from_symbol=midx,
                        name=_text(n.child_by_field_name("name"), source),
                        receiver=recv,
                        receiver_type=None if chained else resolve_receiver(recv, cls, locals_),
                        line=n.start_point[0] + 1,
                        root_type=resolve_receiver(root.group(1), cls, locals_) if root else symbols[cls].name,
                    )
                )
            elif n.type == "synchronized_statement":
                sync_blocks.append((midx, n.start_point[0] + 1))
            elif n.type in ("class_body",):
                continue  # 匿名内部类的方法不算在当前方法里
            stack.extend(reversed(n.children))

    for child in root.children:
        if child.type in TYPE_KINDS:
            visit_type(child, None, package)

    return FileIndex(
        language="java", package=package, imports=imports, symbols=symbols, calls=calls, sync_blocks=sync_blocks
    )
