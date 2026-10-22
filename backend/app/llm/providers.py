"""模型服务商适配层。

只有这里直接接触 SDK（langchain-openai）。上层 client.py 负责重试、降级、计费，
这里只负责"把一次请求发出去并拿回文本和 Token 数"。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.core.config import Settings
from app.llm.types import (
    LLMError,
    LLMFatalError,
    ToolCall,
    ToolSpec,
    ToolStepResult,
    Usage,
)


class Provider(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        json_mode: bool,
        temperature: float,
        task: str,
        payload: dict[str, Any],
    ) -> tuple[str, Usage]: ...

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        task: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[str | Usage]: ...

    async def tool_step(
        self,
        *,
        model: str,
        messages: list[Any],
        tools: list[ToolSpec],
        task: str,
        payload: dict[str, Any],
    ) -> ToolStepResult: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _classify(exc: Exception) -> Exception:
    """把 SDK 异常归成"可重试"和"不可重试"两类，重试策略只看这个。"""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__.lower()
    if status in (429, 500, 502, 503, 504) or "timeout" in name or "connection" in name or "ratelimit" in name:
        return LLMError(f"{type(exc).__name__}: {exc}")
    if isinstance(status, int) and 400 <= status < 500:
        return LLMFatalError(f"{type(exc).__name__}: {exc}")
    return LLMError(f"{type(exc).__name__}: {exc}")


def _to_lc(messages: list[dict[str, Any]]) -> list[Any]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    out: list[Any] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        else:
            out.append(HumanMessage(content=content))
    return out


def _usage(msg: Any) -> Usage:
    meta = getattr(msg, "usage_metadata", None) or {}
    return Usage(prompt_tokens=int(meta.get("input_tokens", 0)), completion_tokens=int(meta.get("output_tokens", 0)))


class OpenAICompatibleProvider:
    """OpenAI 兼容接口（DeepSeek、通义、Kimi、vLLM……都能接）。

    结构化输出用 json_object 模式 + 在提示词里给出 JSON Schema，而不是 OpenAI 专有的
    json_schema strict 模式：兼容的服务商更多（见 D-006）。
    """

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def _chat(self, model: str, temperature: float, **kw: Any) -> Any:
        from langchain_openai import ChatOpenAI

        # max_retries=0：重试由 client.py 统一做，避免 SDK 和我们各重试一遍
        return ChatOpenAI(
            model=model,
            base_url=self.s.llm_base_url or None,
            api_key=self.s.llm_api_key or "none",  # type: ignore[arg-type]
            temperature=temperature,
            timeout=self.s.llm_timeout_s,
            max_retries=0,
            **kw,
        )

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        json_mode: bool,
        temperature: float,
        task: str,
        payload: dict[str, Any],
    ) -> tuple[str, Usage]:
        kw: dict[str, Any] = {}
        if json_mode:
            kw["model_kwargs"] = {"response_format": {"type": "json_object"}}
        try:
            msg = await self._chat(model, temperature, **kw).ainvoke(_to_lc(messages))
        except Exception as e:  # noqa: BLE001 —— 统一分类后再抛
            raise _classify(e) from e
        return str(msg.content), _usage(msg)

    async def stream(  # type: ignore[override]
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        task: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[str | Usage]:
        usage = Usage()
        try:
            async for chunk in self._chat(model, temperature, stream_usage=True).astream(_to_lc(messages)):
                if chunk.content:
                    yield str(chunk.content)
                u = _usage(chunk)
                if u.total:
                    usage = u
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        yield usage

    async def tool_step(
        self,
        *,
        model: str,
        messages: list[Any],
        tools: list[ToolSpec],
        task: str,
        payload: dict[str, Any],
    ) -> ToolStepResult:
        lc_tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]
        # 对话历史里前两条是 dict（system/user），之后是模型返回的 AIMessage 和 ToolMessage
        lc_msgs = _to_lc([m for m in messages if isinstance(m, dict)]) + [
            m for m in messages if not isinstance(m, dict)
        ]
        try:
            msg = await self._chat(model, 0.0).bind_tools(lc_tools).ainvoke(lc_msgs)
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        calls = [
            ToolCall(id=c.get("id") or f"call_{i}", name=c["name"], args=c.get("args") or {})
            for i, c in enumerate(getattr(msg, "tool_calls", []) or [])
        ]
        return ToolStepResult(tool_calls=calls, text=str(msg.content or ""), usage=_usage(msg), assistant_message=msg)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from langchain_openai import OpenAIEmbeddings

        emb = OpenAIEmbeddings(
            model=self.s.embedding_model,
            base_url=self.s.embedding_base_url or None,
            api_key=self.s.embedding_api_key or "none",  # type: ignore[arg-type]
            dimensions=self.s.embedding_dim if "text-embedding-3" in self.s.embedding_model else None,
            check_embedding_ctx_length=False,  # 非 OpenAI 服务商不认 tiktoken 分好的 token 数组
            max_retries=2,
        )
        try:
            return await emb.aembed_documents(texts)
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e


class FakeProvider:
    """假模型：按任务名分派到 fake.py 里的确定性实现。

    为什么不用随机文本：集成测试要断言"有缺口时会追问""阅卷分数落在区间内"，
    所以假模型必须"看得懂"输入（关键词匹配），才能走到图里的各个分支。
    """

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        json_mode: bool,
        temperature: float,
        task: str,
        payload: dict[str, Any],
    ) -> tuple[str, Usage]:
        from app.llm import fake

        result = fake.handle(task, payload)
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        prompt_len = sum(len(str(m.get("content", ""))) for m in messages)
        return text, Usage(prompt_tokens=max(1, prompt_len // 4), completion_tokens=max(1, len(text) // 4))

    async def stream(  # type: ignore[override]
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        task: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[str | Usage]:
        text, usage = await self.complete(
            model=model, messages=messages, json_mode=False, temperature=temperature, task=task, payload=payload
        )
        # 按小片段吐出，模拟真实流式效果
        for i in range(0, len(text), 8):
            yield text[i : i + 8]
        yield usage

    async def tool_step(
        self,
        *,
        model: str,
        messages: list[Any],
        tools: list[ToolSpec],
        task: str,
        payload: dict[str, Any],
    ) -> ToolStepResult:
        from app.llm import fake

        calls, text = fake.tool_step(task, payload, [t.name for t in tools])
        return ToolStepResult(tool_calls=calls, text=text, usage=Usage(prompt_tokens=50, completion_tokens=10))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from app.llm import fake

        return [fake.embed(t) for t in texts]


def make_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "fake":
        return FakeProvider()
    return OpenAICompatibleProvider(settings)
