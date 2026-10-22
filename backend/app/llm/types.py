"""llm 模块内部共享的类型。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Tier = Literal["fast", "strong"]


@dataclass
class CallContext:
    """一次模型调用属于谁、属于哪个会话/任务、在哪个节点。

    计费、配额、trace 都靠它归账。Agent 代码在入口处创建一次，往下传。
    """

    node: str
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    job_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def child(self, node: str) -> CallContext:
        return CallContext(node=node, user_id=self.user_id, session_id=self.session_id, job_id=self.job_id)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolStepResult:
    """agentic 检索一轮的结果：要么继续调用工具，要么给出结束语。"""

    tool_calls: list[ToolCall]
    text: str
    usage: Usage
    assistant_message: Any = None  # 真实模型下是 AIMessage，原样放回对话历史


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class LLMError(Exception):
    """可重试的模型错误（429、5xx、超时）。"""


class LLMFatalError(Exception):
    """不可重试（鉴权失败、请求不合法）。"""


class BudgetExceeded(Exception):
    """会话预算或每日配额用完。"""
