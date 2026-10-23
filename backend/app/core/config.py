"""全局配置。

为什么用 pydantic-settings：环境变量 → 带类型的对象，启动时就校验，
配错了立刻报错，而不是跑到一半才发现某个值是空字符串。
所有可调参数（限制、预算、价格表）都集中在这里，方便在 learn 文档里"改配置验证"。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 代码默认值和 .env.example 里的占位值，生产环境不允许沿用
_DEFAULT_JWT_SECRETS = ("change-me-in-env", "please-change-me-to-a-long-random-string")


class Settings(BaseSettings):
    # 在 backend/ 下运行时，项目根目录和当前目录的 .env 都会读取（后者优先）
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")

    # ---- 运行环境 ----
    env: Literal["dev", "test", "prod"] = "dev"
    # 测试模式下允许 file:// 仓库地址（SPEC 14：集成测试用本地夹具仓库）
    allow_file_repos: bool = False
    log_level: str = "INFO"
    # 允许的前端来源，用于 CSRF 的 Origin 校验
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8090", "http://localhost:5173"]
    )

    # ---- 数据库与缓存 ----
    database_url: str = "postgresql+asyncpg://projectb:projectb@localhost:5433/projectb"
    redis_url: str = "redis://localhost:6380/0"

    # ---- 登录 ----
    jwt_secret: str = "change-me-in-env"
    jwt_expire_hours: int = 24 * 7
    github_client_id: str = ""
    github_client_secret: str = ""
    oauth_redirect_url: str = "http://localhost:8090/api/auth/github/callback"
    frontend_url: str = "http://localhost:8090"
    admin_github_logins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # 仅 dev/test：允许 /api/auth/dev-login 直接以某个登录名登录，方便没配 OAuth 时调试
    enable_dev_login: bool = False

    # ---- 对话模型 ----
    llm_provider: Literal["openai_compatible", "fake"] = "fake"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model_strong: str = "fake-strong"
    llm_model_fast: str = "fake-fast"
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 3

    # ---- 嵌入模型 ----
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "fake-embedding"
    # pgvector 的 HNSW 索引最多支持 2000 维，超了迁移建索引会失败，所以启动时就拦下
    embedding_dim: int = Field(default=256, ge=1, le=2000)
    # 请求里带上 dimensions，让服务商把向量截到 EMBEDDING_DIM；维度固定、不认这个参数的模型（如 bge-m3）设成 false
    embedding_send_dimensions: bool = True
    # 每次请求最多几条：阿里云百炼 10、硅基流动 32、智谱 64，默认取最小的，接哪家都不会超
    embedding_batch_size: int = Field(default=10, ge=1)

    # ---- 计费：{"model": {"input": 每百万输入 Token 价格, "output": 每百万输出 Token 价格}} ----
    llm_price_table: dict[str, dict[str, float]] = Field(default_factory=dict)

    # ---- 仓库与配额 ----
    repo_storage_dir: str = "/data/repos"
    # api 容器以只读方式挂载仓库卷（SPEC 13.2）；需要重新克隆时交给 worker
    repo_storage_readonly: bool = False
    max_repo_mb: int = 200
    max_files: int = 20000
    max_file_kb: int = 512
    clone_timeout_s: int = 180
    storage_limit_gb: float = 10.0
    daily_token_quota: int = 2_000_000
    retrieval_mode: Literal["agentic", "vector", "hybrid"] = "agentic"

    # 任务队列：arq（正常运行）/ inline（测试：在当前进程里用后台协程执行，可 drain）
    queue_mode: Literal["arq", "inline"] = "arq"

    # ---- 会话控制 ----
    session_token_budget: int = 200_000
    max_followup_depth: int = 3
    hint_penalty: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [5, 10, 20])  # 一、二、三级提示扣分
    pass_score: int = 60
    max_concurrent_jobs_per_user: int = 2
    max_active_sessions_per_user: int = 3
    rate_limit_per_minute: int = 120

    # ---- GitHub App（M7）----
    github_app_id: str = ""
    github_app_private_key_path: str = ""

    @field_validator("admin_github_logins", "allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # 环境变量里写 "a,b" 比写 JSON 数组更顺手，两种都支持
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("llm_price_table", mode="before")
    @classmethod
    def _parse_price(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    @field_validator("hint_penalty", mode="before")
    @classmethod
    def _parse_penalty(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace("[", "").replace("]", "").split(",") if x.strip()]
        return v

    @model_validator(mode="after")
    def _check_prod(self) -> Settings:
        # 生产环境用默认或过短的 JWT 密钥等于谁都能伪造登录态：宁可启动失败，也不带着它上线
        weak = self.jwt_secret in _DEFAULT_JWT_SECRETS or len(self.jwt_secret) < 32
        if self.is_prod and weak:
            raise ValueError("ENV=prod 时必须把 JWT_SECRET 设为至少 32 位的随机字符串")
        return self

    @property
    def sync_database_url(self) -> str:
        """psycopg 风格的连接串，给 LangGraph 检查点用（它不走 SQLAlchemy）。"""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
