"""AI 应用画像：规则做确定性抽取，不调用模型（SPEC 7.4.3、D-023）。

和 Spring 画像（spring_profile）的思路一样：先用规则从代码里找出"值得考的地方"和"可疑写法"，
每条都带证据位置，再交给模型改写成考点。识别八类：工具调用、Agent 循环、状态图、多 Agent、
MCP、RAG、上下文与安全、模型调用工程化；外加三条风险规则：循环没有上限、工具没设超时、
工具输出没隔离就拼进提示词。

规则优先看解析出来的结构（调用、装饰器、赋值），少看原文：分析本项目自己时，这个文件里的
正则和注释写满了 bind_tools 之类的词，只看原文会把检测代码本身当成 AI 用法。必须看原文的
几条（SQL 里的向量运算符、递归上限、while 循环）都跳过注释行，正则也写成不会匹配自己的形式。

只分析 Python 文件；测试代码不进画像（和 Spring 画像一样）。
"""

from __future__ import annotations

import re
from typing import Any

from app.analysis.code_model import CodeModel, SymRef
from app.analysis.spring_profile import is_test_path

AI_LIBS = ("langchain", "langgraph", "openai", "anthropic", "mcp", "llama_index", "dashscope", "zhipuai", "litellm")
# 调模型的写法：接收者像模型或客户端，方法名像生成
LLM_RECEIVER = re.compile(r"(llm|model|chat|client|provider|agent_executor|completions|messages)", re.I)
LLM_METHODS = {"invoke", "ainvoke", "stream", "astream", "batch", "abatch", "generate", "agenerate", "create"}
LLM_METHODS |= {"structured", "complete", "tool_step", "chat", "predict"}
EMBED_CALLS = {"embed", "aembed", "embed_documents", "aembed_documents", "embed_query", "aembed_query"}
VECTOR_CALLS = {"cosine_distance", "l2_distance", "max_inner_product", "similarity_search", "asimilarity_search"}
GRAPH_SAVERS = {
    "MemorySaver",
    "InMemorySaver",
    "PostgresSaver",
    "AsyncPostgresSaver",
    "SqliteSaver",
    "AsyncSqliteSaver",
}
MULTI_AGENT_CALLS = {"create_supervisor", "create_swarm", "create_handoff_tool", "Send"}
# 只写 split 太宽（配置里的 _split_csv 也会算进来），要么带 chunk，要么是切文本、文档
SPLITTER = re.compile(r"chunk|split.*(text|doc|markdown|md|page|content)|(text|doc|markdown)s?_?split", re.I)
# 只认循环语句（行尾有冒号），多行列表推导式里单独成行的 for 不算
LOOP_STMT = re.compile(r"^\s*(async\s+)?(for|while)\b.*:\s*$")
RERANK = re.compile(r"(rerank|rrf|bm25|hybrid)", re.I)
NETWORK = re.compile(r"\b(httpx|requests|aiohttp|urllib)\b|\.(get|post|put|request|urlopen)\(")
# 按词匹配：只写 retr 会配到 return，而 while True 里有 return 并不代表有上限
LIMIT_WORDS = re.compile(r"\b\w*(max|limit|budget|attempts?|retry|retries|steps?|rounds?|timeout|deadline)\w*\b", re.I)
# 下面几条要看原文；写成字符类，免得匹配到本文件自己
SQL_VECTOR_OP = re.compile(r"[\"'].*<[=#-]>.*[\"']")
VECTOR_INDEX = re.compile(r"\b(hnsw|ivfflat)\b", re.I)
RECURSION_CFG = re.compile(r"recursion_limit[\"']?\s*[:=]")
WHILE_FOREVER = re.compile(r"^(\s*)while\s+(True|1)\s*:")
TOOLS_ARG = re.compile(r"\btools\s*=\s*[\[\w(]")
TPL_CONST = re.compile(r"^([A-Z][A-Z0-9_]*(?:PROMPT|SYSTEM|TEMPLATE|ROLE|GUARD|INSTRUCTION)[A-Z0-9_]*)\s*(?::[^=]+)?=")
MCP_SERVER = re.compile(r"^(\w+)\s*=\s*(?:\w+\.)*(FastMCP|MCPServer)\(")
MCP_TRANSPORT = re.compile(r"(transport\s*=\s*[\"'](\w[\w-]*)[\"']|stdio_server|sse_app|streamable_http_app)")
CITE_WORDS = re.compile(r"(引用|出处|citation|cite|没有找到|不知道|no relevant|don't know)", re.I)
RAG_NAME = re.compile(r"(search|retriev|rag|cite|citation|answer)", re.I)
ISOLATION_CALLS = re.compile(r"(wrap|escape|sanitize|quote|isolate)", re.I)
MCP_AUTH = re.compile(r"(token|authorization|api[_-]?key|bearer)", re.I)

CATEGORIES = ("tools", "agent_loops", "graphs", "multi_agent", "mcp", "rag", "context", "llm_calls")


def _loc(s: SymRef, line: int | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "symbol": s.short,
        "symbol_id": str(s.id),
        "file": s.path,
        "line": line or s.sym.start_line,
        "start_line": s.sym.start_line,
        "end_line": s.sym.end_line,
        **extra,
    }


def _code_lines(text: str, start: int, end: int) -> list[tuple[int, str]]:
    """[start, end] 行里不是注释的行（带行号）。"""
    lines = text.splitlines()
    return [(i, ln) for i, ln in enumerate(lines[start - 1 : end], start) if not ln.lstrip().startswith("#")]


def _is_ai_file(model_file: Any) -> bool:
    idx = model_file.index
    names = [*idx.imports, *idx.aliases.values()]
    # 按前缀比：langchain_core、langchain_openai、openai_agents 都算
    return any(n.split(".")[0].startswith(AI_LIBS) for n in names)


def _calls_llm(model: CodeModel, s: SymRef) -> bool:
    for rc in model.calls_from(s):
        c = rc.call
        if c.name == "bind_tools" or c.name == "tool_step":
            return True
        if c.name in LLM_METHODS and c.receiver and LLM_RECEIVER.search(c.receiver):
            return True
    return False


def _while_forever_without_limit(lines: list[tuple[int, str]]) -> int | None:
    """while True 的循环体里既没有 break，也没有上限、计数之类的字眼：返回行号。"""
    for k, (lineno, ln) in enumerate(lines):
        m = WHILE_FOREVER.match(ln)
        if not m:
            continue
        indent = len(m.group(1))
        body = []
        for _, b in lines[k + 1 :]:
            if b.strip() and len(b) - len(b.lstrip()) <= indent:
                break
            body.append(b)
        text = "\n".join(body)
        if "break" not in text and not LIMIT_WORDS.search(text):
            return lineno
    return None


def extract_ai_profile(model: CodeModel, sources: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in (*CATEGORIES, "risks")}
    funcs = [
        s
        for s in model.symbols
        if s.file.index.language == "python" and s.sym.kind in ("function", "method") and not is_test_path(s.path)
    ]
    # 状态图的节点名、MCP Server 的变量名要按文件汇总
    mcp_vars: dict[str, dict[str, Any]] = {}
    for f in model.files:
        if f.index.language != "python" or is_test_path(f.path):
            continue
        text = sources.get(f.path, "")
        for lineno, ln in _code_lines(text, 1, text.count("\n") + 1):
            m = MCP_SERVER.match(ln)
            if m:
                transport = next((t.group(0) for t in MCP_TRANSPORT.finditer(text)), None)
                mcp_vars[f.path] = {"var": m.group(1), "line": lineno}
                # 先找真正带上凭据的代码（请求头），找不到再退而求其次找提到 token 的行
                code = _code_lines(text, 1, text.count("\n") + 1)
                auth = next((n for n, a in code if re.search(r"authorization|bearer", a, re.I)), None)
                auth = auth or next((n for n, a in code if MCP_AUTH.search(a)), None)
                if auth:
                    out["mcp"].append(
                        {
                            "symbol": "鉴权",
                            "symbol_id": None,
                            "file": f.path,
                            "line": auth,
                            "start_line": auth,
                            "end_line": auth,
                            "kind": "auth",
                        }
                    )
                out["mcp"].append(
                    {
                        "symbol": m.group(1),
                        "symbol_id": None,
                        "file": f.path,
                        "line": lineno,
                        "start_line": lineno,
                        "end_line": lineno,
                        "kind": "server",
                        "detail": f"{m.group(2)}；传输：{transport or '默认'}",
                    }
                )
            t = TPL_CONST.match(ln)
            if t:
                out["context"].append(
                    {
                        "symbol": t.group(1),
                        "symbol_id": None,
                        "file": f.path,
                        "line": lineno,
                        "start_line": lineno,
                        "end_line": lineno,
                        "kind": "prompt_template",
                    }
                )
            # 只认建索引的语句（CREATE INDEX ... USING hnsw、postgresql_using="hnsw"），不认随便提到这个词的地方
            if VECTOR_INDEX.search(ln) and re.search(r"using", ln, re.I):
                out["rag"].append(
                    {
                        "symbol": "向量索引",
                        "symbol_id": None,
                        "file": f.path,
                        "line": lineno,
                        "start_line": lineno,
                        "end_line": lineno,
                        "kind": "vector_index",
                    }
                )
            if RECURSION_CFG.search(ln):
                out["agent_loops"].append(
                    {
                        "symbol": "recursion_limit",
                        "symbol_id": None,
                        "file": f.path,
                        "line": lineno,
                        "start_line": lineno,
                        "end_line": lineno,
                        "kind": "recursion_limit",
                    }
                )

    for s in funcs:
        text = sources.get(s.path, "")
        lines = _code_lines(text, s.sym.start_line, s.sym.end_line)
        body = "\n".join(ln for _, ln in lines)
        calls = model.calls_from(s)
        names = {rc.call.name for rc in calls}
        anns = [a.name for a in s.sym.annotations]
        llm = _calls_llm(model, s)

        # ---- 工具调用：@tool 注册、bind_tools、tools=[...]
        if any(a in ("tool", "function_tool") or a.endswith(("tools.tool", ".function_tool")) for a in anns):
            has_timeout = "timeout" in body
            network = bool(NETWORK.search(body))
            out["tools"].append(
                _loc(
                    s,
                    kind="tool",
                    detail=f"超时：{'有' if has_timeout else '没有'}；异常处理：{'有' if 'except' in body else '没有'}",
                )
            )
            if network and not has_timeout:
                out["risks"].append(
                    _loc(
                        s,
                        rule="tool_no_timeout",
                        title=f"工具 {s.sym.name} 发网络请求却没设超时",
                        detail="对方不响应时，整个 Agent 会一直卡在这次工具调用上",
                    )
                )
        for rc in calls:
            if rc.call.name == "bind_tools":
                out["tools"].append(_loc(s, rc.call.line, kind="bind_tools"))
        tools_arg = next((n for n, ln in lines if TOOLS_ARG.search(ln)), None)
        if tools_arg and llm:
            out["tools"].append(_loc(s, tools_arg, kind="tools_param"))

        # ---- Agent 循环：调模型的函数里有循环，并且处理 tool_calls
        loop_line = next((n for n, ln in lines if LOOP_STMT.match(ln)), None)
        if loop_line and llm and ("tool_calls" in body or "tool_use" in body):
            limit = next((n for n, ln in lines if LIMIT_WORDS.search(ln) and ("range(" in ln or "if " in ln)), None)
            out["agent_loops"].append(
                _loc(
                    s,
                    loop_line,
                    kind="agent_loop",
                    detail=f"终止条件在第 {limit} 行" if limit else "没找到明确的上限",
                )
            )
        forever = _while_forever_without_limit(lines)
        if forever and _is_ai_file(s.file):
            out["risks"].append(
                _loc(
                    s,
                    forever,
                    rule="loop_no_limit",
                    title=f"{s.short} 里的 while True 没有上限",
                    detail="循环体里没有 break，也没有步数、预算或超时之类的限制，外部条件一直不满足就停不下来",
                )
            )

        # ---- 状态图：StateGraph 的构建函数、节点、条件边、中断、检查点
        if "StateGraph" in names:
            nodes = re.findall(r"add_node\(\s*[\"'](\w+)[\"']", body)
            nodes += re.findall(r"add_(?:conditional_)?edges?\(\s*[\"'](\w+)[\"']", body)
            out["graphs"].append(
                _loc(
                    s,
                    kind="graph",
                    nodes=sorted(set(nodes)),
                    detail=f"条件边 {sum(1 for rc in calls if rc.call.name == 'add_conditional_edges')} 处",
                )
            )
        for rc in calls:
            if rc.call.name == "interrupt" and rc.call.receiver is None:
                out["graphs"].append(_loc(s, rc.call.line, kind="interrupt"))
            elif rc.call.name in GRAPH_SAVERS:
                out["graphs"].append(_loc(s, rc.call.line, kind="checkpointer", detail=rc.call.name))
            elif rc.call.name in MULTI_AGENT_CALLS:
                out["multi_agent"].append(_loc(s, rc.call.line, kind=rc.call.name))

        # ---- MCP：Server 上注册的 tool / resource / prompt
        server = mcp_vars.get(s.path)
        if server and any(
            a in (f"{server['var']}.tool", f"{server['var']}.resource", f"{server['var']}.prompt") for a in anns
        ):
            kind = next(a for a in anns if a.startswith(server["var"] + ".")).split(".", 1)[1]
            out["mcp"].append(_loc(s, kind=kind))

        # ---- RAG：分块、嵌入、向量检索、重排、引用和拒答
        if SPLITTER.search(s.sym.name):
            out["rag"].append(_loc(s, kind="chunking"))
        for rc in calls:
            c = rc.call
            if c.name in EMBED_CALLS or (c.name == "create" and (c.receiver or "").endswith("embeddings")):
                out["rag"].append(_loc(s, c.line, kind="embedding"))
            elif c.name in VECTOR_CALLS:
                out["rag"].append(_loc(s, c.line, kind="vector_search"))
        sql_line = next((n for n, ln in lines if SQL_VECTOR_OP.search(ln)), None)
        if sql_line:
            out["rag"].append(_loc(s, sql_line, kind="vector_search"))
        if RERANK.search(s.sym.name):
            out["rag"].append(_loc(s, kind="rerank"))
        # 引用和拒答：自己做检索，或者调了仓库里的检索函数（answer_with_citations 调 search）的地方
        rag_fn = bool(sql_line) or any(rc.call.name in EMBED_CALLS | VECTOR_CALLS for rc in calls)
        calls_search = any(rc.targets and RAG_NAME.search(rc.call.name) for rc in calls)
        if rag_fn or (RAG_NAME.search(s.sym.name) and calls_search):
            cite = next((n for n, ln in lines if CITE_WORDS.search(ln)), None)
            if cite:
                out["rag"].append(_loc(s, cite, kind="citation_or_refusal"))

        # ---- 上下文与安全：截断、隔离，以及"工具输出直接拼进提示词"
        for rc in calls:
            if rc.call.name in ("truncate", "trim_messages"):
                out["context"].append(_loc(s, rc.call.line, kind="truncation"))
            elif ISOLATION_CALLS.search(rc.call.name) and rc.call.receiver is None:
                out["context"].append(_loc(s, rc.call.line, kind="isolation", detail=rc.call.name))
        isolated = any(ISOLATION_CALLS.search(rc.call.name) for rc in calls)
        for n, ln in lines:
            const = re.search(r"\b([A-Z][A-Z0-9_]*(?:PROMPT|SYSTEM|TEMPLATE))\b", ln)
            if const and "+" in ln and re.search(r"\+\s*\w+\(", ln) and not isolated:
                out["risks"].append(
                    _loc(
                        s,
                        n,
                        rule="tool_output_in_prompt",
                        title=f"{s.short} 把函数返回的内容直接拼进了 {const.group(1)}",
                        detail="外部内容（工具输出、网页、检索结果）没有隔离就进了指令里，里面写一句忽略之前的指令就能改掉模型的行为",
                    )
                )
                break

        # ---- 调用工程化：调模型的封装里有没有重试、超时、降级、记账
        if llm:
            flags = {
                "重试": bool(re.search(r"retr|attempt|backoff|tenacity", body, re.I)),
                "超时": "timeout" in body,
                "降级": bool(re.search(r"fallback|降级|degrade", body, re.I)),
                "记账": bool(re.search(r"usage|token|cost|record", body, re.I)),
            }
            if any(flags.values()):
                out["llm_calls"].append(_loc(s, kind="llm_call", detail="、".join(k for k, v in flags.items() if v)))
        if s.sym.name.endswith(("with_retry", "with_fallback")):
            out["llm_calls"].append(_loc(s, kind="wrapper"))

    # 同一位置不重复；每类最多留 30 条，够出题用，也不让画像变得太大
    for k, items in out.items():
        seen: set[tuple[Any, ...]] = set()
        uniq = []
        for it in items:
            key = (it["file"], it["line"], it.get("kind"), it.get("rule"))
            if key not in seen:
                seen.add(key)
                uniq.append(it)
        out[k] = uniq[:30]
    return out
