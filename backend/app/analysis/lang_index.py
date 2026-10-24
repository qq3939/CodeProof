"""与语言无关的符号索引接口（SPEC 7.3、D-022）。

v1 的代码模型、画像、改动理解都直接依赖 Java 的解析结果（JavaFileIndex）。加 Python 时
如果再写一套平行的流程，两边很快会不一致。这里把"一个文件解析出什么"定成统一结构：
符号（类、函数、方法、字段）、注解或装饰器、调用。每种语言只负责把源码解析成这个结构，
后面的代码模型、链路、考点都只认这个结构。

表结构不用改：code_symbols.kind 是字符串，annotations 是 JSONB，加语言不用清库。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

TYPE_KINDS = {"class", "interface", "enum", "record", "annotation"}
# 可被调用的符号：Java 的方法和构造器，Python 的函数（模块级或嵌套）和方法
CALLABLE_KINDS = {"method", "constructor", "function"}


@dataclass
class Annotation:
    """Java 注解或 Python 装饰器。

    name：Java 存简单名（Transactional）；Python 存点号全名（router.post、mcp.tool），
    装饰器的接收者本身就有意义（是哪个路由器、哪个 MCP Server）。
    """

    name: str
    args: str  # 括号里的原文，如 propagation = Propagation.REQUIRES_NEW
    line: int


@dataclass
class Symbol:
    kind: str  # class/interface/enum/record/annotation/method/constructor/field/function
    name: str
    qualified_name: str
    signature: str
    start_line: int
    end_line: int
    annotations: list[Annotation] = field(default_factory=list)
    parent: int | None = None  # 在 symbols 列表里的下标
    # 类型级：父类和接口；字段级：字段类型；方法级：返回类型
    type_text: str | None = None
    supertypes: list[str] = field(default_factory=list)
    # 方法级：参数名 → 类型，用来解析调用接收者
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class Call:
    from_symbol: int  # 所在方法在 symbols 里的下标
    name: str  # 被调用的方法名
    receiver: str | None  # 接收者原文，如 userService、this.repo、self.llm
    receiver_type: str | None  # 推断出的接收者类型（简单名）
    line: int
    # 链式调用 a.b().c() 时，最左边变量 a 的类型。画像识别 Redis 等用法时用它
    root_type: str | None = None


@dataclass
class FileIndex:
    language: str
    package: str  # Java 的包名；Python 的模块名（app.sessions.graph）
    imports: list[str]  # 导入的全名：Java 的 com.a.B；Python 的 a.b.c（from a.b import c）
    symbols: list[Symbol]
    calls: list[Call]
    # 方法体里出现的 synchronized / lock() 等并发写法（画像用）
    sync_blocks: list[tuple[int, int]] = field(default_factory=list)  # (方法下标, 行号)
    # 本地名 → 导入的全名（Python 的 import x as y、from a import b），解析 y.f() 这类调用用
    aliases: dict[str, str] = field(default_factory=dict)
    # 框架层面的事实，按语言各自约定（Python：FastAPI 的路由器、include_router、Depends）
    extras: dict[str, Any] = field(default_factory=dict)


Parser = Callable[[bytes, str], FileIndex]


def _parse_java(source: bytes, path: str) -> FileIndex:
    from app.analysis.java_index import parse_java

    return parse_java(source)


def _parse_python(source: bytes, path: str) -> FileIndex:
    from app.analysis.python_index import parse_python

    return parse_python(source, path)


PARSERS: dict[str, tuple[str, Parser]] = {
    ".java": ("java", _parse_java),
    ".py": ("python", _parse_python),
}
INDEXED_LANGS = {lang for lang, _ in PARSERS.values()}


def parser_for(path: str) -> tuple[str, Parser] | None:
    """按扩展名找解析器；没有专门支持的语言返回 None（走通用兜底，SPEC 7.4.4）。"""
    return PARSERS.get(PurePosixPath(path).suffix.lower())


def index_from_dict(d: dict[str, Any]) -> FileIndex:
    """从解析缓存的 JSON 还原（缓存按 dataclasses.asdict 写入）。"""
    syms = [Symbol(**{**s, "annotations": [Annotation(**a) for a in s["annotations"]]}) for s in d["symbols"]]
    return FileIndex(
        language=d["language"],
        package=d["package"],
        imports=d["imports"],
        symbols=syms,
        calls=[Call(**c) for c in d["calls"]],
        sync_blocks=[(int(a), int(b)) for a, b in d["sync_blocks"]],
        aliases=d.get("aliases", {}),
        extras=d.get("extras", {}),
    )
