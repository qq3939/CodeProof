"""Spring 仓库画像：纯确定性抽取，不调用模型（SPEC 7.4）。

为什么不让模型直接读仓库写画像：
- 确定性抽取可复现、零成本，同一个提交永远得到同一份画像，方便缓存和评测；
- 接口、注解、依赖这些是"硬事实"，用语法树拿比让模型猜准确得多；
- 模型只在后面"挑考点、出题"时用，拿到的是已经整理好的事实。
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from app.analysis.code_model import CodeModel, SymRef
from app.analysis.fastapi_profile import extract_fastapi_endpoints

HTTP_ANN = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": None,
}
COMPONENT_ANN = {
    "Service": "service",
    "Component": "component",
    "Repository": "repository",
    "Mapper": "mapper",
    "RestController": "controller",
    "Controller": "controller",
    "Configuration": "configuration",
}
REDIS_TYPES = {
    "RedisTemplate",
    "StringRedisTemplate",
    "ReactiveRedisTemplate",
    "RedissonClient",
    "RLock",
    "RMap",
    "RBucket",
    "RAtomicLong",
    "RBloomFilter",
    "RRateLimiter",
    "ValueOperations",
    "ZSetOperations",
    "HashOperations",
    "ListOperations",
    "SetOperations",
    "Jedis",
    "JedisPool",
    "RedisUtil",
    "RedisClient",
}
MQ_TYPES = {"RabbitTemplate", "AmqpTemplate", "KafkaTemplate", "RocketMQTemplate", "StreamBridge", "JmsTemplate"}
MQ_LISTENERS = {"RabbitListener", "KafkaListener", "RocketMQMessageListener", "JmsListener", "StreamListener"}
LOCK_TYPES = {"ReentrantLock", "Lock", "ReadWriteLock", "ReentrantReadWriteLock", "StampedLock", "RLock", "Semaphore"}
POOL_RE = re.compile(
    r"new\s+(ThreadPoolExecutor|ThreadPoolTaskExecutor|ScheduledThreadPoolExecutor|ForkJoinPool)\b|Executors\.new\w+"
)
DATA_LAYER_HINT = re.compile(r"(Mapper|Repository|Dao|DAO)$")

TECH_MAP = [
    ("spring-boot-starter-web", "Spring MVC"),
    ("spring-boot-starter-webflux", "Spring WebFlux"),
    ("mybatis-plus", "MyBatis-Plus"),
    ("mybatis", "MyBatis"),
    ("spring-boot-starter-data-jpa", "Spring Data JPA"),
    ("hibernate", "Hibernate"),
    ("mysql", "MySQL"),
    ("postgresql", "PostgreSQL"),
    ("h2", "H2"),
    ("spring-boot-starter-data-redis", "Redis"),
    ("jedis", "Redis"),
    ("redisson", "Redisson"),
    ("spring-boot-starter-amqp", "RabbitMQ"),
    ("spring-kafka", "Kafka"),
    ("kafka", "Kafka"),
    ("rocketmq", "RocketMQ"),
    ("spring-boot-starter-cache", "Spring Cache"),
    ("caffeine", "Caffeine"),
    ("elasticsearch", "Elasticsearch"),
    ("spring-boot-starter-security", "Spring Security"),
    ("jjwt", "JWT"),
    ("sa-token", "Sa-Token"),
    ("spring-cloud", "Spring Cloud"),
    ("nacos", "Nacos"),
    ("sentinel", "Sentinel"),
    ("dubbo", "Dubbo"),
    ("xxl-job", "XXL-JOB"),
    ("quartz", "Quartz"),
    ("minio", "MinIO"),
    ("thymeleaf", "Thymeleaf"),
    ("spring-boot-starter-validation", "Bean Validation"),
    ("lombok", "Lombok"),
    ("mongodb", "MongoDB"),
    ("websocket", "WebSocket"),
    ("spring-ai", "Spring AI"),
    ("langchain4j", "LangChain4j"),
    # Python（D-022）：需要带上分隔符的写法避免误配，如 langchain- 不会配到 langchain4j
    ("fastapi", "FastAPI"),
    ("django", "Django"),
    ("flask", "Flask"),
    ("sqlalchemy", "SQLAlchemy"),
    ("pydantic", "Pydantic"),
    ("celery", "Celery"),
    ("langgraph", "LangGraph"),
    ("langchain-", "LangChain"),
    ("llama-index", "LlamaIndex"),
    ("mcp", "MCP"),
    ("pgvector", "pgvector"),
    ("chromadb", "Chroma"),
    ("pymilvus", "Milvus"),
    ("faiss", "FAISS"),
    ("openai", "OpenAI SDK"),
    ("anthropic", "Anthropic SDK"),
]


TEST_PATH = re.compile(r"(^|/)(src/test|test|tests)/|Tests?\.java$|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")


def is_test_path(path: str) -> bool:
    """测试代码照样建索引（工具能查到），但不进画像和考点：面试不会考测试方法里的 @Transactional。"""
    return bool(TEST_PATH.search(path))


@dataclass
class TextFile:
    path: str
    text: str


# ---------------------------------------------------------------- 依赖与配置
def parse_dependencies(build_files: list[TextFile]) -> tuple[str | None, list[str]]:
    build: str | None = None
    deps: list[str] = []
    for f in build_files:
        if f.path.endswith("pom.xml"):
            build = build or "maven"
            for m in re.finditer(r"<dependency>(.*?)</dependency>", f.text, re.S):
                g = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", m.group(1))
                a = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", m.group(1))
                if a:
                    deps.append(f"{g.group(1) if g else ''}:{a.group(1)}")
        elif f.path.endswith((".gradle", ".gradle.kts")):
            build = build or "gradle"
            for m in re.finditer(
                r"(?:implementation|api|compileOnly|runtimeOnly|annotationProcessor)\s*\(?\s*['\"]([^'\"]+)['\"]",
                f.text,
            ):
                parts = m.group(1).split(":")
                if len(parts) >= 2:
                    deps.append(f"{parts[0]}:{parts[1]}")
        elif f.path.endswith("pyproject.toml"):
            build = build or "pyproject"
            # [project] dependencies = ["fastapi>=0.110", ...] 和 poetry 的 [tool.poetry.dependencies] 两种写法
            block = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", f.text)
            if block:
                deps += [_py_dep(x) for x in re.findall(r"[\"']([^\"']+)[\"']", block.group(1))]
            poetry = re.search(r"(?ms)^\[tool\.poetry\.dependencies\](.*?)(^\[|\Z)", f.text)
            if poetry:
                deps += [m for m in re.findall(r"(?m)^([A-Za-z0-9_.\-]+)\s*=", poetry.group(1)) if m != "python"]
        elif re.search(r"requirements[^/]*\.txt$", f.path):
            build = build or "pip"
            for line in f.text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    deps.append(_py_dep(line))
    return build, sorted({d for d in deps if d})


def _py_dep(spec: str) -> str:
    """fastapi[standard]>=0.110 → fastapi；包名统一小写、下划线换成连字符。"""
    m = re.match(r"\s*([A-Za-z0-9_.\-]+)", spec)
    return m.group(1).lower().replace("_", "-") if m else ""


def tech_stack(deps: list[str]) -> list[str]:
    out: list[str] = []
    joined = " ".join(deps).lower()
    for needle, label in TECH_MAP:
        if needle in joined and label not in out:
            out.append(label)
    return out


def _flatten_yaml(text: str) -> list[tuple[str, int]]:
    """极简 YAML 键展开：只按缩进拼出 a.b.c 形式的键名，不解析值。

    为什么不用 PyYAML：我们只需要键名（值一律脱敏），而且配置文件里可能有
    Spring 占位符、多文档分隔符等，完整解析反而容易报错。少一个依赖。
    """
    keys: list[tuple[str, int]] = []
    stack: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith(("#", "-")) or line.strip() == "---":
            if line.strip() == "---":
                stack = []
            continue
        m = re.match(r"^(\s*)([A-Za-z0-9_.\-\[\]\"']+)\s*:(.*)$", line)
        if not m:
            continue
        indent, key, rest = len(m.group(1)), m.group(2).strip("\"'"), m.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full = ".".join([k for _, k in stack] + [key])
        if rest and not rest.startswith(("|", ">")):
            keys.append((full, lineno))
        else:
            stack.append((indent, key))
    return keys


def parse_config_keys(config_files: list[TextFile]) -> list[dict[str, Any]]:
    """只记录配置键和位置，值一律写成 ***（SPEC 7.4：配置值一律脱敏）。"""
    out: list[dict[str, Any]] = []
    for f in config_files:
        if f.path.endswith((".yml", ".yaml")):
            for k, line in _flatten_yaml(f.text):
                out.append({"file": f.path, "key": k, "line": line, "value": "***"})
        elif f.path.endswith(".properties"):
            for lineno, raw in enumerate(f.text.splitlines(), 1):
                line = raw.strip()
                if not line or line.startswith(("#", "!")):
                    continue
                k = re.split(r"\s*[=:]\s*", line, maxsplit=1)[0]
                out.append({"file": f.path, "key": k, "line": lineno, "value": "***"})
    return out


# ---------------------------------------------------------------- 接口
def _mapping_paths(args: str) -> list[str]:
    if not args:
        return [""]
    # @GetMapping("/x")、@GetMapping(value = {"/a", "/b"})、@RequestMapping(path = "/x", method = ...)
    m = re.search(r"(?:value|path)\s*=\s*(\{[^}]*\}|\"[^\"]*\")", args)
    target = m.group(1) if m else args.split(",")[0] if not re.match(r"\s*\w+\s*=", args) else ""
    paths = re.findall(r"\"([^\"]*)\"", target)
    return paths or [""]


def _request_methods(args: str) -> list[str]:
    ms = re.findall(r"RequestMethod\.(\w+)", args)
    return ms or ["ANY"]


def _join(prefix: str, path: str) -> str:
    full = "/" + "/".join(p.strip("/") for p in (prefix, path) if p.strip("/"))
    return full if full != "" else "/"


def extract_endpoints(model: CodeModel) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in model.symbols:
        if t.sym.kind != "class" or not t.has_ann("RestController", "Controller"):
            continue
        prefixes = [p for a in t.ann("RequestMapping") for p in _mapping_paths(a)] or [""]
        for m in model.methods_of(t):
            for a in m.sym.annotations:
                if a.name not in HTTP_ANN:
                    continue
                methods = [HTTP_ANN[a.name]] if HTTP_ANN[a.name] else _request_methods(a.args)
                for prefix in prefixes:
                    for path in _mapping_paths(a.args):
                        for hm in methods:
                            out.append(
                                {
                                    "method": hm,
                                    "path": _join(prefix, path),
                                    "handler": m.short,
                                    "symbol_id": str(m.id),
                                    "file": m.path,
                                    "line": m.sym.start_line,
                                    "end_line": m.sym.end_line,
                                }
                            )
    out.sort(key=lambda e: (e["path"], e["method"]))
    return out


# ---------------------------------------------------------------- 组件与关键用法
def _loc(s: SymRef, line: int | None = None) -> dict[str, Any]:
    return {
        "symbol": s.short,
        "symbol_id": str(s.id),
        "file": s.path,
        "line": line or s.sym.start_line,
        "start_line": s.sym.start_line,
        "end_line": s.sym.end_line,
    }


def extract_components(model: CodeModel) -> list[dict[str, Any]]:
    out = []
    for t in model.symbols:
        if t.sym.kind not in ("class", "interface") or is_test_path(t.path):
            continue
        for a in t.sym.annotations:
            if a.name in COMPONENT_ANN:
                out.append({"kind": COMPONENT_ANN[a.name], "name": t.sym.name, **_loc(t)})
                break
    return out


def extract_usages(model: CodeModel, sources: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    u: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in model.symbols:
        if s.sym.kind not in ("method", "class") or is_test_path(s.path):
            continue
        for a in s.sym.annotations:
            if a.name == "Transactional":
                prop = re.search(r"Propagation\.(\w+)", a.args)
                rb = re.search(r"rollbackFor\s*=\s*([^,)]+)", a.args)
                u["transaction"].append(
                    {
                        **_loc(s, a.line),
                        "propagation": prop.group(1) if prop else "REQUIRED",
                        "rollback_for": rb.group(1).strip() if rb else None,
                        "args": a.args,
                    }
                )
            elif a.name in ("Cacheable", "CacheEvict", "CachePut", "Caching"):
                u["cache"].append({**_loc(s, a.line), "annotation": a.name, "args": a.args})
            elif a.name in MQ_LISTENERS:
                u["mq"].append({**_loc(s, a.line), "kind": "listener", "annotation": a.name, "args": a.args})
            elif a.name == "Scheduled":
                u["scheduled"].append({**_loc(s, a.line), "args": a.args})
            elif a.name == "Async":
                u["async"].append({**_loc(s, a.line), "args": a.args})
        if s.sym.kind == "method" and re.search(r"\bsynchronized\b", s.sym.signature):
            u["concurrency"].append({**_loc(s), "kind": "synchronized_method"})
    seen: set[tuple[str, str, str]] = set()
    for rc in model.calls:
        if is_test_path(rc.caller.path):
            continue
        root = rc.call.root_type or ""
        kind = None
        if root in REDIS_TYPES or rc.call.name.startswith("opsFor"):
            kind = "redis"
        elif root in MQ_TYPES:
            kind = "mq"
        if kind:
            key = (kind, str(rc.caller.id), rc.call.name)
            if key in seen:
                continue
            seen.add(key)
            entry = {
                **_loc(rc.caller, rc.call.line),
                "call": f"{rc.call.receiver or ''}.{rc.call.name}".lstrip("."),
                "receiver_type": root,
            }
            if kind == "mq":
                entry["kind"] = "producer"
            u[kind].append(entry)
        if root in LOCK_TYPES and rc.call.name in ("lock", "tryLock", "unlock", "lockInterruptibly"):
            key = ("lock", str(rc.caller.id), root)
            if key not in seen:
                seen.add(key)
                u["concurrency"].append(
                    {
                        **_loc(rc.caller, rc.call.line),
                        "kind": "distributed_lock" if root == "RLock" else "lock",
                        "call": rc.call.name,
                        "receiver_type": root,
                    }
                )
    for f in model.files:
        if is_test_path(f.path):
            continue
        for midx, line in f.index.sync_blocks:
            m = model.by_id[f.symbol_ids[midx]]
            u["concurrency"].append({**_loc(m, line), "kind": "synchronized_block"})
        text = sources.get(f.path, "")
        for mm in POOL_RE.finditer(text):
            line = text.count("\n", 0, mm.start()) + 1
            owner = next(
                (
                    s
                    for s in model.symbols
                    if s.file is f and s.sym.kind == "method" and s.sym.start_line <= line <= s.sym.end_line
                ),
                None,
            )
            if owner:
                u["concurrency"].append({**_loc(owner, line), "kind": "thread_pool", "call": mm.group(0)})
    return dict(u)


# ---------------------------------------------------------------- 链路
PY_DATA_HINT = re.compile(r"(repositor|dao|crud|models?|db|database|store|storage)", re.I)


def _layer(s: SymRef) -> str:
    if s.file.index.language == "python":
        # Python 没有 @Service 这类注解，只能按模块路径猜分层
        p = s.path.lower()
        if PY_DATA_HINT.search(p):
            return "data"
        if "service" in p:
            return "business"
        return "other"
    owner = s.owner
    if owner is None:
        return "other"
    if owner.has_ann("RestController", "Controller"):
        return "entry"
    if owner.has_ann("Mapper", "Repository") or DATA_LAYER_HINT.search(owner.sym.name):
        return "data"
    if owner.has_ann("Service", "Component") or owner.sym.name.endswith(("Service", "ServiceImpl", "Manager")):
        return "business"
    return "other"


def build_chains(model: CodeModel, endpoints: list[dict[str, Any]], max_depth: int = 4) -> list[dict[str, Any]]:
    """从接口处理方法出发沿调用关系最多走 4 层（SPEC 7.4）。

    遇到接口方法就展开到实现类；遇到 Redis/MQ 等中间件调用记为叶子站点。
    BFS 而不是 DFS：同一层的站点按调用顺序排列，读起来就是"先做什么、再做什么"。
    """
    chains = []
    for ep in endpoints:
        start = model.by_id.get(uuid.UUID(ep["symbol_id"]))
        if start is None:
            continue
        steps: list[dict[str, Any]] = [{"depth": 0, "layer": "entry", **_loc(start)}]
        visited = {start.id}
        # FastAPI 的 Depends：框架在调用处理函数之前先调它们，算作入口后的第一站（D-022）
        for dep in ep.get("depends", []):
            ref = model.by_id.get(uuid.UUID(dep["symbol_id"])) if dep.get("symbol_id") else None
            if ref is not None and ref.id not in visited:
                visited.add(ref.id)
                steps.append({"depth": 1, "layer": "dependency", **_loc(ref), "from": start.short})
        frontier = [start]
        for depth in range(1, max_depth + 1):
            nxt: list[SymRef] = []
            for cur in frontier:
                for rc in sorted(model.calls_from(cur), key=lambda r: r.call.line):
                    root = rc.call.root_type or ""
                    if not rc.targets and (
                        root in REDIS_TYPES or root in MQ_TYPES or rc.call.name.startswith("opsFor")
                    ):
                        steps.append(
                            {
                                "depth": depth,
                                "layer": "middleware",
                                "symbol": f"{root}.{rc.call.name}",
                                "symbol_id": None,
                                "file": cur.path,
                                "line": rc.call.line,
                                "start_line": rc.call.line,
                                "end_line": rc.call.line,
                                "from": cur.short,
                            }
                        )
                        continue
                    for t in rc.targets:
                        for impl in model.expand_impl(t):
                            if impl.id in visited:
                                continue
                            visited.add(impl.id)
                            steps.append(
                                {
                                    "depth": depth,
                                    "layer": _layer(impl),
                                    **_loc(impl),
                                    "from": cur.short,
                                    "call_line": rc.call.line,
                                }
                            )
                            nxt.append(impl)
            frontier = nxt
            if not frontier:
                break
        chains.append({"endpoint": f"{ep['method']} {ep['path']}", "handler": ep["handler"], "steps": steps})
    return chains


# ---------------------------------------------------------------- 汇总
def build_profile(
    model: CodeModel,
    build_files: list[TextFile],
    config_files: list[TextFile],
    sources: dict[str, str],
    lang_stats: dict[str, int],
) -> dict[str, Any]:
    build, deps = parse_dependencies(build_files)
    # 测试代码里的接口不进画像（D-011）：v1 只在关键用法里排除了测试代码，接口没排除，
    # 本项目自己的测试夹具 mini-feed 的接口进了画像摘要，模型据此编出了"点赞链路"的考点（P-018）
    endpoints = sorted(
        (e for e in extract_endpoints(model) + extract_fastapi_endpoints(model) if not is_test_path(e["file"])),
        key=lambda e: (e["path"], e["method"]),
    )
    components = extract_components(model)
    usages = extract_usages(model, sources)
    chains = build_chains(model, endpoints)
    # 放在函数里导入：ai_profile 要用这里的 is_test_path，模块级互相导入会循环
    from app.analysis.ai_profile import extract_ai_profile

    ai = extract_ai_profile(model, sources)
    modules: Counter[str] = Counter()
    for f in model.files:
        pkg = f.index.package.split(".")
        if f.index.language == "python":
            # Python 取模块名前两段（app.sessions），再往下就是单个文件了
            modules[".".join(pkg[:2]) or "(root)"] += 1
        else:
            # 取包名第 4 段作为"模块"（com.example.app.<module>），短包名就取最后一段
            modules[pkg[3] if len(pkg) > 3 else (pkg[-1] if pkg and pkg[0] else "(default)")] += 1
    return {
        "stack": {"build": build, "dependencies": deps, "tech": tech_stack(deps)},
        "languages": lang_stats,
        "config_keys": parse_config_keys(config_files),
        "modules": [{"name": k, "files": v} for k, v in modules.most_common()],
        "endpoints": endpoints,
        "components": components,
        "usages": usages,
        "chains": chains,
        "ai": ai,
        "stats": {
            "files": len(model.files),
            "files_by_language": dict(Counter(f.index.language for f in model.files)),
            "types": sum(1 for s in model.symbols if s.sym.kind in ("class", "interface", "enum", "record")),
            "methods": sum(1 for s in model.symbols if s.sym.kind in ("method", "function")),
            "calls": len(model.calls),
            "resolved_calls": sum(1 for c in model.calls if c.targets),
        },
    }


AI_LABELS = {
    "tools": "工具调用",
    "agent_loops": "Agent 循环",
    "graphs": "状态图",
    "multi_agent": "多 Agent",
    "mcp": "MCP",
    "rag": "RAG",
    "context": "上下文与安全",
    "llm_calls": "调用工程化",
}


def profile_summary_md(profile: dict[str, Any]) -> str:
    st = profile["stack"]
    lines = [
        "## 技术栈",
        "",
        f"- 构建工具：{st['build'] or '未知'}",
        f"- 技术：{'、'.join(st['tech']) or '未识别'}",
        "",
        f"## 接口（{len(profile['endpoints'])} 个）",
        "",
    ]
    for e in profile["endpoints"][:50]:
        lines.append(f"- `{e['method']} {e['path']}` → `{e['handler']}`（{e['file']}:{e['line']}）")
    lines += ["", "## 关键用法", ""]
    names = {
        "transaction": "事务",
        "cache": "缓存",
        "redis": "Redis",
        "mq": "消息",
        "scheduled": "定时任务",
        "async": "异步",
        "concurrency": "并发",
    }
    for k, label in names.items():
        items = profile["usages"].get(k, [])
        if items:
            lines.append(
                f"- {label}：{len(items)} 处，例如 `{items[0]['symbol']}`（{items[0]['file']}:{items[0]['line']}）"
            )
    ai = profile.get("ai") or {}
    if any(ai.get(k) for k in AI_LABELS):
        lines += ["", "## AI 应用画像", ""]
        for k, label in AI_LABELS.items():
            items = ai.get(k) or []
            if items:
                it = items[0]
                lines.append(f"- {label}：{len(items)} 处，例如 `{it['symbol']}`（{it['file']}:{it['line']}）")
        for r in ai.get("risks") or []:
            lines.append(f"- 风险：{r['title']}（{r['file']}:{r['line']}）")
    s = profile["stats"]
    names = {"java": "Java", "python": "Python"}
    by_lang = s.get("files_by_language") or {"java": s["files"]}
    files = "、".join(f"{names.get(k, k)} {v} 个" for k, v in sorted(by_lang.items(), key=lambda x: -x[1]))
    lines += [
        "",
        f"已建索引的源码文件：{files}；共 {s['types']} 个类型、{s['methods']} 个函数或方法；"
        f"调用 {s['calls']} 处，其中 {s['resolved_calls']} 处解析到了仓库内的符号。",
    ]
    return "\n".join(lines)
