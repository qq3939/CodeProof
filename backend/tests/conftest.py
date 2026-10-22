"""测试夹具。

- 假模型（LLM_PROVIDER=fake），不需要任何 API Key；
- Postgres + pgvector、Redis 用 testcontainers 启动（SPEC 14）；
  本机已有实例时可以设置 TEST_DATABASE_URL / TEST_REDIS_URL 跳过容器，加快速度；
- 任务队列用 inline 模式：后台任务在测试进程里跑，用 drain() 等它们结束；
- 测试仓库是 tests/fixtures/mini-feed，复制到临时目录后 git init，用 file:// 地址提交。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "mini-feed"
_TMP = Path(tempfile.mkdtemp(prefix="pb-test-"))

# 必须在 import app 之前设置：models.py 在导入时读取向量维度
os.environ.update(
    {
        "ENV": "test",
        "LLM_PROVIDER": "fake",
        "QUEUE_MODE": "inline",
        "ALLOW_FILE_REPOS": "true",
        "ENABLE_DEV_LOGIN": "true",
        "EMBEDDING_DIM": "256",
        "REPO_STORAGE_DIR": str(_TMP / "repos"),
        "RETRIEVAL_MODE": "hybrid",
        "ADMIN_GITHUB_LOGINS": "admin",
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "LLM_PRICE_TABLE": '{"fake-strong": {"input": 2.0, "output": 8.0}, "fake-fast": {"input": 0.5, "output": 1.0}}',
    }
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(name: str = "mini-feed") -> Path:
    dest = _TMP / "src" / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURE, dest)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "init")
    return dest


def commit_change(repo: Path, rel: str, old: str, new: str, msg: str = "change") -> str:
    p = repo / rel
    p.write_text(p.read_text().replace(old, new))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="session")
def infra() -> Iterator[dict[str, str]]:
    db_url = os.environ.get("TEST_DATABASE_URL")
    redis_url = os.environ.get("TEST_REDIS_URL")
    containers = []
    if not db_url:
        from testcontainers.postgres import PostgresContainer

        pg = PostgresContainer("pgvector/pgvector:pg16", username="pb", password="pb", dbname="pb", driver="asyncpg")
        pg.start()
        containers.append(pg)
        db_url = pg.get_connection_url()
    if not redis_url:
        from testcontainers.redis import RedisContainer

        rc = RedisContainer("redis:7-alpine")
        rc.start()
        containers.append(rc)
        redis_url = f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"
    os.environ["DATABASE_URL"] = db_url
    os.environ["REDIS_URL"] = redis_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield {"db": db_url, "redis": redis_url}
    for c in containers:
        c.stop()


@pytest.fixture(scope="session")
async def app_ready(infra: dict[str, str]) -> AsyncIterator[None]:
    from alembic.config import Config

    from alembic import command
    from app.core.db import reset_engine
    from app.core.redis import get_redis, reset_redis

    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "alembic"))
    # 每次测试从干净的库开始：先降级再升级（env.py 内部用 asyncio.run，所以放到线程里执行）
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    # 检查点表不归 Alembic 管，也删掉重建：保证每次都覆盖"全新数据库第一次启动"这条路径
    from sqlalchemy import text

    from app.core.db import get_engine
    from app.sessions.graph import setup_checkpointer

    async with get_engine().begin() as conn:
        for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations"):
            await conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
    await setup_checkpointer()
    await reset_engine()
    await reset_redis()
    await get_redis().flushdb()
    yield
    from app.sessions.graph import close_checkpointer

    await close_checkpointer()
    await reset_engine()
    await reset_redis()


@pytest.fixture(scope="session")
async def app_client(app_ready: None) -> AsyncIterator[object]:
    import httpx

    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client: object, login_name: str) -> object:
    import httpx

    from app.main import app

    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    r = await c.post("/api/auth/dev-login", json={"login": login_name})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="session")
async def user_client(app_ready: None) -> AsyncIterator[object]:
    c = await login(None, "alice")
    yield c
    await c.aclose()  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
async def admin_client(app_ready: None) -> AsyncIterator[object]:
    c = await login(None, "admin")
    yield c
    await c.aclose()  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
async def analyzed(user_client: object) -> dict[str, object]:
    """提交夹具仓库并等分析完成，后面的集成测试共用这个快照。"""
    from app.core.queue import drain

    repo = make_repo()
    r = await user_client.post("/api/repos", json={"url": f"file://{repo}"})  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    await drain()
    data = r.json()
    snap = (await user_client.get(f"/api/snapshots/{data['snapshot_id']}")).json()  # type: ignore[attr-defined]
    assert snap["status"] == "ready", snap
    return {"repo_path": repo, **data, "snapshot": snap}


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    shutil.rmtree(_TMP, ignore_errors=True)
