"""所有模型结构化输出的 Pydantic 定义。

模型输出一律按这些结构校验（SPEC 8.6），不合法就带错误重试。
字段说明写在 description 里，会进入 JSON Schema，等于同时给模型看了"字段文档"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    path: str = Field(description="仓库内相对路径")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


# ---------------------------------------------------------------- 考点与题目
TopicKind = Literal["design", "risk", "chain", "mechanism", "change"]


class TopicOut(BaseModel):
    kind: TopicKind
    title: str = Field(max_length=200)
    description: str
    evidence: list[Evidence] = Field(min_length=1, max_length=5)
    importance: int = Field(ge=1, le=5)
    topic_key: str = Field(description="归一化的考点键，如 count.redis-batch、chain:GET /api/feed")
    identifiers: list[str] = Field(default_factory=list, description="证据片段里必须出现的标识符")


class TopicList(BaseModel):
    topics: list[TopicOut]


class KeyPoint(BaseModel):
    point: str = Field(description="一个答案要点，一句话")
    evidence: list[Evidence] = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list, description="命中该要点通常会提到的词")


class QuestionOut(BaseModel):
    text: str
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    key_points: list[KeyPoint] = Field(min_length=2, max_length=5)
    followup_hints: list[str] = Field(default_factory=list, description="追问方向，不能直接包含答案")
    reference_md: str = Field(description="参考讲解，Markdown")


class QuestionList(BaseModel):
    questions: list[QuestionOut] = Field(min_length=1, max_length=3)


# ---------------------------------------------------------------- 面试官
AssessCategory = Literal["complete", "partial", "wrong", "off_topic", "unknown"]


class QuickAssessOut(BaseModel):
    category: AssessCategory
    missing_points: list[int] = Field(default_factory=list, description="还没答到的要点下标（从 0 开始）")
    code_claims: list[str] = Field(default_factory=list, description="回答里关于代码的具体说法，需要去核对")
    reasoning: str = ""


class HintOut(BaseModel):
    hint: str


# ---------------------------------------------------------------- 阅卷
class PointResult(BaseModel):
    index: int
    verdict: Literal["hit", "partial", "miss"]
    reason: str


class Contradiction(BaseModel):
    claim: str
    evidence: list[Evidence] = Field(default_factory=list)
    explanation: str


class GradeOut(BaseModel):
    points: list[PointResult]
    contradictions: list[Contradiction] = Field(default_factory=list)
    score: int = Field(ge=0, le=100)
    feedback: str
    review_topics: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 简历
ClaimType = Literal["tech", "effect", "metric"]


class ClaimOut(BaseModel):
    text: str
    type: ClaimType
    entities: list[str] = Field(default_factory=list, description="类名、技术名、注解、配置键等可检索的实体")


class ResumeProjectOut(BaseModel):
    name: str
    claims: list[ClaimOut]


class ResumeExtractOut(BaseModel):
    projects: list[ResumeProjectOut]


class QueryPlan(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=8)


class VerdictOut(BaseModel):
    verdict: Literal["supported", "partial", "not_found", "contradicted"]
    evidence: list[Evidence] = Field(default_factory=list)
    explanation: str
    suggestion: str
