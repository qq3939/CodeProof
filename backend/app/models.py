"""全部数据表（SPEC 第 6 节）。

为什么放在一个文件里：表之间外键关系多，放一起读起来能一眼看清全貌；
各模块的 repository 层再按需 import。
主键统一用 UUID：分布式友好、不泄露数据量，也方便在 URL 里直接用。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.db import Base

EMBED_DIM = get_settings().embedding_dim


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped[Any]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable)


# ---------------------------------------------------------------- 用户与认证
class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    login: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # user / admin
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- 仓库与快照
class Repository(Base):
    __tablename__ = "repositories"
    id: Mapped[uuid.UUID] = _uuid_pk()
    url: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)  # 规范化后的地址
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(200))
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _created()


class RepoSnapshot(Base):
    """一个仓库的一个提交 = 一个快照。UNIQUE(repository_id, commit_sha) 是去重的关键（见 D-002）。"""

    __tablename__ = "repo_snapshots"
    __table_args__ = (UniqueConstraint("repository_id", "commit_sha", name="uq_snapshot_repo_sha"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = _fk("repositories.id")
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(200))
    # queued → cloning → indexing → profiling → generating → ready / failed
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    file_count: Mapped[int | None] = mapped_column(Integer)
    lang_stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 增量分析：基于哪个旧快照、复用了多少
    base_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserRepo(Base):
    __tablename__ = "user_repos"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    latest_snapshot_id: Mapped[uuid.UUID | None] = _fk("repo_snapshots.id", nullable=True, ondelete="SET NULL")
    ref: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------- 代码索引
class CodeFile(Base):
    __tablename__ = "code_files"
    __table_args__ = (Index("ix_code_files_snapshot_path", "snapshot_id", "path"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = _fk("repo_snapshots.id")
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    language: Mapped[str | None] = mapped_column(String(30))
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lines: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()


class CodeSymbol(Base):
    __tablename__ = "code_symbols"
    __table_args__ = (
        Index("ix_code_symbols_snapshot_name", "snapshot_id", "name"),
        Index("ix_code_symbols_snapshot_qname", "snapshot_id", "qualified_name"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = _fk("repo_snapshots.id")
    file_id: Mapped[uuid.UUID] = _fk("code_files.id")
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # class/interface/enum/record/method/field
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    qualified_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    annotations: Mapped[list[Any] | None] = mapped_column(JSONB)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()


class CodeRef(Base):
    __tablename__ = "code_refs"
    __table_args__ = (Index("ix_code_refs_snapshot_to", "snapshot_id", "to_symbol_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = _fk("repo_snapshots.id")
    from_symbol_id: Mapped[uuid.UUID] = _fk("code_symbols.id")
    to_name: Mapped[str] = mapped_column(String(300), nullable=False)
    receiver: Mapped[str | None] = mapped_column(String(300))
    to_symbol_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()


class CodeChunk(Base):
    __tablename__ = "code_chunks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = _fk("repo_snapshots.id")
    file_id: Mapped[uuid.UUID] = _fk("code_files.id")
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = _created()


class RepoProfile(Base):
    __tablename__ = "repo_profiles"
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repo_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    summary_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created()


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (Index("ix_topics_snapshot_kind", "snapshot_id", "kind"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = _fk("repo_snapshots.id")
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # design/risk/chain/mechanism/change
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    importance: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    topic_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created()


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (Index("ix_questions_snapshot_mode", "snapshot_id", "mode"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = _fk("repo_snapshots.id")
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    followup_hints: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    reference_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    topic_key: Mapped[str | None] = mapped_column(String(200))
    # 链路学习 / 改动理解 / 简历追问 这类按会话临时生成的题，标记归属，避免混进共享题库
    scope: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(100))
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- 简历
class Resume(Base):
    __tablename__ = "resumes"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    filename: Mapped[str | None] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="parsed", nullable=False)
    # {project_name: snapshot_id}
    mapping: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class ResumeClaim(Base):
    __tablename__ = "resume_claims"
    id: Mapped[uuid.UUID] = _uuid_pk()
    resume_id: Mapped[uuid.UUID] = _fk("resumes.id")
    project_name: Mapped[str] = mapped_column(String(300), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(20), nullable=False)  # tech / effect / metric
    entities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()


class ClaimVerification(Base):
    __tablename__ = "claim_verifications"
    id: Mapped[uuid.UUID] = _uuid_pk()
    claim_id: Mapped[uuid.UUID] = _fk("resume_claims.id")
    # supported / partial / not_found / contradicted / unverifiable
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggestion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- 考核会话
class ExamSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_started", "user_id", "started_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()  # 同时是 LangGraph 的 thread_id
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    snapshot_id: Mapped[uuid.UUID] = _fk("repo_snapshots.id")
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # active / grading / finished / failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    finish_reason: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class SessionTurn(Base):
    __tablename__ = "session_turns"
    __table_args__ = (Index("ix_turns_session_seq", "session_id", "seq"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = _fk("sessions.id")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # examiner/user/hint/system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    question_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class Grading(Base):
    __tablename__ = "gradings"
    __table_args__ = (UniqueConstraint("session_id", "question_id", name="uq_grading_session_question"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = _fk("sessions.id")
    question_id: Mapped[uuid.UUID] = _fk("questions.id")
    point_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    contradictions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    raw_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    hint_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    feedback_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_topics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    judge_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    agreement: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _created()


class Report(Base):
    __tablename__ = "reports"
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    summary_md: Mapped[str] = mapped_column(Text, nullable=False)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    weak_topics: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()


class Mastery(Base):
    __tablename__ = "mastery"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    topic_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(300))
    score_ema: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 连续 >80 次数
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- 知识库
class KbDocument(Base):
    __tablename__ = "kb_documents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(300))
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()


class KbChunk(Base):
    __tablename__ = "kb_chunks"
    __table_args__ = (Index("ix_kb_chunks_user", "user_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = _fk("kb_documents.id")
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    heading_path: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- 计费、Trace、评测
class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created", "created_at"), Index("ix_llm_calls_session", "session_id"))
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[str | None] = mapped_column(String(100))
    node: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")  # ok/error/degraded
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (Index("ix_agent_steps_session_step", "session_id", "step"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[str | None] = mapped_column(String(100))
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(100))
    tool_args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_preview: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    suite: Mapped[str] = mapped_column(String(100), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    report_path: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    """后台任务记录：arq 自己的结果只存在 Redis 里且会过期，
    运维页需要看"排队/运行/失败"并能重试，所以在 Postgres 里留一份。"""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status", "status"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(50), nullable=False)  # analyze/grade/report/resume_verify/kb_ingest
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")  # queued/running/done/failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
