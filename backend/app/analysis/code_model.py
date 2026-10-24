"""整个仓库的内存代码模型 + 近似调用关系解析（SPEC 7.3）。

流水线先把每个文件解析成与语言无关的 FileIndex（见 lang_index），再在这里汇总成全仓库视图：
给每个符号分配 UUID、按名字建索引、把调用解析到候选符号。
画像、链路、考点都基于这个模型计算，最后一次性写库。

近似解析的局限（见 D-002）：只看"方法名 + 接收者类型 + 同包/导入"，
接口多实现、重载、反射、动态代理时可能连错或连不上；Python 的鸭子类型、运行时赋值、
装饰器包装会让解析率更低（D-022）。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from app.analysis.lang_index import CALLABLE_KINDS, TYPE_KINDS, Call, FileIndex, Symbol


@dataclass
class FileEntry:
    id: uuid.UUID
    path: str
    index: FileIndex
    symbol_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class SymRef:
    id: uuid.UUID
    file: FileEntry
    sym: Symbol
    owner: SymRef | None  # 所在类型（方法/字段的类，内部类的外部类）

    @property
    def path(self) -> str:
        return self.file.path

    @property
    def short(self) -> str:
        """Class.method 形式的短名，给人看。"""
        if self.owner is not None and self.sym.kind not in TYPE_KINDS:
            return f"{self.owner.sym.name}.{self.sym.name}"
        return self.sym.name

    def ann(self, name: str) -> list[str]:
        """返回某个注解的参数原文列表（可能出现多次）。"""
        return [a.args for a in self.sym.annotations if a.name == name]

    def has_ann(self, *names: str) -> bool:
        return any(a.name in names for a in self.sym.annotations)


@dataclass
class ResolvedCall:
    caller: SymRef
    call: Call
    targets: list[SymRef]


class CodeModel:
    def __init__(self, files: list[FileEntry]) -> None:
        self.files = files
        self.symbols: list[SymRef] = []
        self.by_id: dict[uuid.UUID, SymRef] = {}
        self.types_by_name: dict[str, list[SymRef]] = defaultdict(list)
        self.methods_by_name: dict[str, list[SymRef]] = defaultdict(list)
        self.calls: list[ResolvedCall] = []
        # 接口/父类简单名 → 实现类
        self.implementations: dict[str, list[SymRef]] = defaultdict(list)
        self._calls_from: dict[uuid.UUID, list[ResolvedCall]] = defaultdict(list)
        self._callers: dict[uuid.UUID, list[ResolvedCall]] = defaultdict(list)
        self._build()
        self._resolve()

    # ------------------------------------------------------------ 建索引
    def _build(self) -> None:
        for f in self.files:
            local: list[SymRef] = []
            f.symbol_ids = []
            for s in f.index.symbols:
                owner = local[s.parent] if s.parent is not None else None
                ref = SymRef(id=uuid.uuid4(), file=f, sym=s, owner=owner)
                local.append(ref)
                f.symbol_ids.append(ref.id)
                self.symbols.append(ref)
                self.by_id[ref.id] = ref
                if s.kind in TYPE_KINDS:
                    self.types_by_name[s.name].append(ref)
                    for sup in s.supertypes:
                        self.implementations[sup].append(ref)
                elif s.kind in CALLABLE_KINDS:
                    self.methods_by_name[s.name].append(ref)

    def _file_symbol(self, f: FileEntry, idx: int) -> SymRef:
        return self.by_id[f.symbol_ids[idx]]

    def subtypes(self, type_name: str) -> set[str]:
        """type_name 的所有（传递）子类型简单名，含自身。"""
        out = {type_name}
        frontier = [type_name]
        while frontier:
            t = frontier.pop()
            for impl in self.implementations.get(t, []):
                if impl.sym.name not in out:
                    out.add(impl.sym.name)
                    frontier.append(impl.sym.name)
        return out

    def supertypes_of(self, type_name: str) -> set[str]:
        """type_name 的所有（传递）父类型简单名，不含自身。Python 的 self.f() 常常调的是父类方法。"""
        out: set[str] = set()
        frontier = [type_name]
        while frontier:
            t = frontier.pop()
            for ref in self.types_by_name.get(t, []):
                for sup in ref.sym.supertypes:
                    if sup not in out:
                        out.add(sup)
                        frontier.append(sup)
        return out

    @staticmethod
    def qualified(m: SymRef) -> str:
        """导入时用的全名：Java 的 包.类；Python 模块级函数的 模块.函数。"""
        if m.owner is not None:
            return f"{m.file.index.package}.{m.owner.sym.name}"
        return f"{m.file.index.package}.{m.sym.name}"

    # ------------------------------------------------------------ 解析调用
    def _resolve(self) -> None:
        for f in self.files:
            for c in f.index.calls:
                caller = self._file_symbol(f, c.from_symbol)
                rc = ResolvedCall(caller=caller, call=c, targets=self._targets(caller, c))
                self.calls.append(rc)
                self._calls_from[caller.id].append(rc)
                for t in rc.targets:
                    self._callers[t.id].append(rc)

    def _targets(self, caller: SymRef, c: Call) -> list[SymRef]:
        cands = [m for m in self.methods_by_name.get(c.name, []) if m.sym.kind in ("method", "function")]
        if not cands:
            return []
        idx = caller.file.index
        owner_name = caller.owner.sym.name if caller.owner else None
        if c.receiver_type:
            # 接收者类型已知：只要这个类型（或其实现类、父类）里的同名方法
            exact = [m for m in cands if m.owner and m.owner.sym.name == c.receiver_type]
            if exact:
                return exact[:3]
            subs = self.subtypes(c.receiver_type)
            sub = [m for m in cands if m.owner and m.owner.sym.name in subs]
            if sub:
                return sub[:3]
            sups = self.supertypes_of(c.receiver_type)
            return [m for m in cands if m.owner and m.owner.sym.name in sups][:1]
        if c.receiver is None and idx.language == "java":
            # Java 里不写接收者就是调本类方法；Python 调本类方法必须写 self.，不写的是模块级函数
            same = [m for m in cands if m.owner and m.owner.sym.name == owner_name]
            if same:
                return same[:1]
        if c.receiver is not None and c.receiver in idx.aliases:
            # Python 的 模块别名.函数()：到被导入的那个模块里找模块级函数
            target = idx.aliases[c.receiver]
            return [m for m in cands if m.owner is None and m.file.index.package == target][:1]
        if c.receiver is not None:
            # 链式调用、表达式接收者：类型推断不出来，宁可不连也不要乱连
            return []
        # Python 的裸调用只可能是函数（模块级或嵌套），不可能是某个类的方法
        if idx.language == "python":
            cands = [m for m in cands if m.owner is None or m.owner.sym.kind not in TYPE_KINDS]
        same_pkg = [m for m in cands if m.file.index.package == idx.package]
        if same_pkg:
            return same_pkg[:1]
        imports = set(idx.imports)
        return [m for m in cands if self.qualified(m) in imports][:1]

    # ------------------------------------------------------------ 查询
    def calls_from(self, sym: SymRef) -> list[ResolvedCall]:
        return self._calls_from.get(sym.id, [])

    def callers_of(self, sym: SymRef) -> list[ResolvedCall]:
        return self._callers.get(sym.id, [])

    def expand_impl(self, method: SymRef) -> list[SymRef]:
        """接口方法 → 各实现类里的同名方法（链路遍历用）。"""
        if method.owner is None or method.owner.sym.kind != "interface":
            return [method]
        subs = self.subtypes(method.owner.sym.name) - {method.owner.sym.name}
        impls = [m for m in self.methods_by_name.get(method.sym.name, []) if m.owner and m.owner.sym.name in subs]
        return impls or [method]

    def methods_of(self, type_ref: SymRef) -> list[SymRef]:
        return [s for s in self.symbols if s.owner is not None and s.owner.id == type_ref.id and s.sym.kind == "method"]
